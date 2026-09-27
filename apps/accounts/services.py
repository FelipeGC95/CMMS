import datetime
import secrets

from allauth.account.models import EmailAddress
from django.conf import settings
from django.contrib.auth.hashers import make_password
from django.core.mail import send_mail
from django.db import transaction
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from django.utils.text import slugify
from django.utils.translation import gettext as _

from apps.audit import services as audit
from apps.audit.models import AuditEvent
from apps.core.middleware import SESSION_KEY
from apps.core.tenancy import tenant_context
from apps.organizations.models import Location, Organization, Plan
from apps.organizations.plans import ensure_default_plans

from .models import Invitation, Membership, MembershipLocation, PendingSignup, User
from .permissions import Perm, PermissionDeniedError, require_perm
from .selectors import hash_token, pending_invitations_qs

INVITATION_TTL = datetime.timedelta(days=7)


class TeamError(ValueError):
    """Regla de negocio del equipo incumplida; el mensaje se muestra al usuario."""


@transaction.atomic
def create_user(*, email, password=None, full_name="", email_verified=False, **extra):
    """Crea el usuario y su correo en allauth.

    `email_verified=True` solo cuando la plataforma ya confía en el correo
    (datos demo, alta hecha por el superadmin, enlace de invitación); si no,
    allauth lo verifica al primer inicio de sesión.
    """
    user = User.objects.create_user(email, password, full_name=full_name, **extra)
    EmailAddress.objects.create(user=user, email=user.email, primary=True, verified=email_verified)
    return user


def switch_organization(*, request, organization_id):
    """Cambia la organización activa si el usuario es miembro; la recuerda para la próxima sesión."""
    membership = next((m for m in request.memberships if str(m.organization_id) == organization_id), None)
    if membership is None:
        return False
    # Rotar la sesión evita que quede algo de la organización anterior (ORG-07 CA2).
    request.session.cycle_key()
    request.session[SESSION_KEY] = str(membership.organization_id)
    User.objects.filter(pk=request.user.pk).update(last_organization=membership.organization_id)
    return True


# ---------- Invitaciones (ORG-05) ----------


def _check_seat_available(organization, *, exclude_email=""):
    """Cupo de usuarios del plan: miembros activos + invitaciones pendientes (ORG-05 CA3)."""
    plan = organization.plan
    if plan is None or plan.max_users is None:
        return
    used = Membership.objects.filter(organization=organization, is_active=True).count()
    used += pending_invitations_qs(organization).exclude(email=exclude_email).count()
    if used >= plan.max_users:
        raise TeamError(
            _(
                "Tu plan %(plan)s permite hasta %(max)s usuarios, contando las invitaciones "
                "pendientes. Revoca una invitación, desactiva a alguien o cambia de plan."
            )
            % {"plan": plan.name, "max": plan.max_users}
        )


def _send_invitation_email(invitation, accept_url):
    context = {
        "invitation": invitation,
        "organization": invitation.organization,
        "inviter": invitation.invited_by,
        "accept_url": accept_url,
        "expires_days": INVITATION_TTL.days,
    }
    subject = render_to_string("accounts/email/invitation_subject.txt", context).strip()
    body = render_to_string("accounts/email/invitation_message.txt", context)
    send_mail(subject, body, settings.DEFAULT_FROM_EMAIL, [invitation.email])


@transaction.atomic
def invite_member(*, membership, email, role, base_url):
    """Invita por correo con un rol. El enlace dura 7 días y es de un solo uso (token guardado como hash)."""
    require_perm(membership, Perm.TEAM_MANAGE)
    return _issue_invitation(
        organization_id=membership.organization_id,
        email=email,
        role=role,
        invited_by=membership.user,
        base_url=base_url,
    )


@transaction.atomic
def invite_first_admin(*, actor, organization, email, base_url):
    """Invitación del primer administrador al dar de alta la organización (ORG-01 CA2).

    Solo la emite el superadmin de plataforma (ORG-01 CA3); no requiere membresía.
    """
    if actor is None or not actor.is_superuser:
        raise PermissionDeniedError("platform_admin")
    return _issue_invitation(
        organization_id=organization.pk,
        email=email,
        role=Membership.Role.ADMIN,
        invited_by=actor,
        base_url=base_url,
    )


def _issue_invitation(*, organization_id, email, role, invited_by, base_url):
    """Crea la invitación y encola el correo; se llama dentro de una transacción."""
    if role not in Membership.Role.values:
        raise TeamError(_("Rol no válido."))
    # Bloquea la organización: dos invitaciones simultáneas no superan el cupo.
    organization = Organization.objects.select_for_update().get(pk=organization_id)
    email = email.strip().lower()
    if Membership.objects.filter(
        organization=organization, user__email__iexact=email, is_active=True
    ).exists():
        raise TeamError(_("%(email)s ya es parte del equipo.") % {"email": email})
    _check_seat_available(organization, exclude_email=email)

    now = timezone.now()
    # Reenviar reemplaza la invitación anterior: solo un enlace vigente por correo.
    pending_invitations_qs(organization).filter(email=email).update(revoked_at=now)
    token = secrets.token_urlsafe(32)
    invitation = Invitation.objects.create(
        organization=organization,
        email=email,
        role=role,
        token_hash=hash_token(token),
        invited_by=invited_by,
        expires_at=now + INVITATION_TTL,
    )
    with tenant_context(organization):
        audit.record(
            action=AuditEvent.Action.INVITE,
            target=invitation,
            actor=invited_by,
            changes={"email": email, "role": role},
        )
    accept_url = base_url.rstrip("/") + reverse("invitation_accept", args=[token])
    transaction.on_commit(lambda: _send_invitation_email(invitation, accept_url))
    return invitation


def revoke_invitation(*, membership, invitation_id):
    """Revoca una invitación pendiente de la organización activa (lanza DoesNotExist si no)."""
    require_perm(membership, Perm.TEAM_MANAGE)
    invitation = pending_invitations_qs(membership.organization).get(pk=invitation_id)
    invitation.revoked_at = timezone.now()
    invitation.save(update_fields=["revoked_at", "updated_at"])
    audit.record(
        action=AuditEvent.Action.INVITE_REVOKE,
        target=invitation,
        membership=membership,
        changes={"email": invitation.email},
    )
    return invitation


@transaction.atomic
def accept_invitation(*, invitation, user):
    """Agrega (o reactiva) la membresía del usuario con el rol de la invitación (ORG-05 CA2)."""
    invitation = Invitation.objects.select_for_update().select_related("organization").get(pk=invitation.pk)
    if not invitation.is_pending:
        raise TeamError(_("Esta invitación ya no es válida."))
    if user.email.lower() != invitation.email.lower():
        raise TeamError(_("Esta invitación es para %(email)s.") % {"email": invitation.email})

    membership, created = Membership.objects.get_or_create(
        user=user, organization=invitation.organization, defaults={"role": invitation.role}
    )
    if not created:
        membership.role = invitation.role
        membership.is_active = True
        membership.save(update_fields=["role", "is_active", "updated_at"])
    invitation.accepted_at = timezone.now()
    invitation.save(update_fields=["accepted_at", "updated_at"])
    User.objects.filter(pk=user.pk).update(last_organization=invitation.organization)
    # Abrir el enlace enviado a ese correo demuestra que es suyo.
    EmailAddress.objects.filter(user=user, email__iexact=invitation.email).update(verified=True)
    with tenant_context(invitation.organization):
        audit.record(
            action=AuditEvent.Action.INVITE_ACCEPT,
            target=membership,
            actor=user,
            changes={"invitation": str(invitation.pk), "role": membership.role},
        )
    return membership


@transaction.atomic
def register_from_invitation(*, invitation, full_name, password):
    """Crea la cuenta del invitado sin cuenta previa y lo une a la organización."""
    if User.objects.filter(email__iexact=invitation.email).exists():
        raise TeamError(_("Ya existe una cuenta con este correo: ingresa para aceptar la invitación."))
    user = create_user(email=invitation.email, password=password, full_name=full_name, email_verified=True)
    accept_invitation(invitation=invitation, user=user)
    return user


# ---------- Rol, alcance y estado de los miembros (ORG-06) ----------


def _ensure_other_admin(target):
    others = Membership.objects.filter(
        organization_id=target.organization_id, role=Membership.Role.ADMIN, is_active=True
    ).exclude(pk=target.pk)
    if not others.exists():
        raise TeamError(_("La organización necesita al menos un administrador activo."))


@transaction.atomic
def update_member(*, membership, target, role, restricted_to_locations, locations):
    """Cambia rol y alcance por sitio. Requiere el contexto de tenant de la organización."""
    require_perm(membership, Perm.TEAM_MANAGE)
    if target.organization_id != membership.organization_id:
        raise TeamError(_("Miembro no encontrado."))
    if role not in Membership.Role.values:
        raise TeamError(_("Rol no válido."))
    if target.role == Membership.Role.ADMIN and role != Membership.Role.ADMIN:
        _ensure_other_admin(target)
    location_ids = {location.pk for location in locations}
    # El manager de Location filtra por la organización activa: una ubicación ajena no aparece.
    if Location.objects.filter(pk__in=location_ids).count() != len(location_ids):
        raise TeamError(_("Ubicación no válida."))
    if (restricted_to_locations or role == Membership.Role.EXTERNAL) and not location_ids:
        raise TeamError(_("Elige al menos una ubicación para limitar el acceso."))

    before = {
        "role": target.role,
        "restricted_to_locations": target.restricted_to_locations,
        "locations": sorted(
            str(pk)
            for pk in MembershipLocation.objects.filter(membership=target).values_list(
                "location_id", flat=True
            )
        ),
    }
    target.role = role
    target.restricted_to_locations = restricted_to_locations
    target.save(update_fields=["role", "restricted_to_locations", "updated_at"])
    # El alcance es configuración, no historial: se reemplaza.
    MembershipLocation.objects.filter(membership=target).exclude(location_id__in=location_ids).delete()
    existing = set(MembershipLocation.objects.filter(membership=target).values_list("location_id", flat=True))
    for location_id in location_ids - existing:
        MembershipLocation.objects.create(membership=target, location_id=location_id)
    after = {
        "role": role,
        "restricted_to_locations": restricted_to_locations,
        "locations": sorted(str(pk) for pk in location_ids),
    }
    if changes := audit.diff(before, after):
        audit.record(
            action=AuditEvent.Action.ROLE_CHANGE, target=target, membership=membership, changes=changes
        )
    return target


@transaction.atomic
def set_member_active(*, membership, target, active):
    """Desactiva o reactiva un miembro; nunca se borra (conserva su historial)."""
    require_perm(membership, Perm.TEAM_MANAGE)
    if target.organization_id != membership.organization_id:
        raise TeamError(_("Miembro no encontrado."))
    if target.is_active == active:
        return target
    if not active:
        if target.pk == membership.pk:
            raise TeamError(_("No puedes desactivar tu propia cuenta."))
        if target.role == Membership.Role.ADMIN:
            _ensure_other_admin(target)
    else:
        organization = Organization.objects.select_for_update().get(pk=target.organization_id)
        _check_seat_available(organization, exclude_email=target.user.email)
    target.is_active = active
    target.save(update_fields=["is_active", "updated_at"])
    audit.record(
        action=AuditEvent.Action.REACTIVATE if active else AuditEvent.Action.DEACTIVATE,
        target=target,
        membership=membership,
        changes={"is_active": [not active, active]},
    )
    return target


# ---------- Autoregistro con prueba gratuita (ORG-02) ----------

SIGNUP_TTL = datetime.timedelta(days=2)
TRIAL_DAYS = 30


class SignupError(ValueError):
    """El enlace de registro no sirve (vencido, usado o correo ya registrado)."""


def _unique_slug(name: str) -> str:
    base = (slugify(name) or "organizacion")[:40].strip("-") or "organizacion"
    slug, n = base, 2
    while Organization.objects.filter(slug=slug).exists():
        slug = f"{base}-{n}"
        n += 1
    return slug


def _send_signup_email(template: str, email: str, context: dict):
    subject = render_to_string(f"accounts/email/{template}_subject.txt", context).strip()
    body = render_to_string(f"accounts/email/{template}_message.txt", context)
    send_mail(subject, body, settings.DEFAULT_FROM_EMAIL, [email])


@transaction.atomic
def request_signup(*, email, full_name, organization_name, password, confirm_url, login_url):
    """Guarda la solicitud y envía el enlace de verificación. No crea datos todavía (CA2).

    Si el correo ya tiene cuenta se le avisa por correo, no en pantalla (no se revela
    qué correos están registrados). `confirm_url(token)` arma el enlace de verificación.
    """
    email = email.strip().lower()
    if User.objects.filter(email__iexact=email).exists():
        _send_signup_email("signup_existing", email, {"login_url": login_url})
        return None
    # Una solicitud nueva reemplaza a las anteriores sin completar del mismo correo.
    PendingSignup.objects.filter(email=email, completed_at__isnull=True).delete()
    token = secrets.token_urlsafe(32)
    pending = PendingSignup.objects.create(
        email=email,
        full_name=full_name.strip(),
        organization_name=organization_name.strip(),
        password_hash=make_password(password),
        token_hash=hash_token(token),
        expires_at=timezone.now() + SIGNUP_TTL,
    )
    _send_signup_email(
        "signup_confirm",
        email,
        {"pending": pending, "confirm_url": confirm_url(token), "expires_days": SIGNUP_TTL.days},
    )
    return pending


def pending_signup_by_token(token):
    return PendingSignup.objects.filter(
        token_hash=hash_token(token), completed_at__isnull=True, expires_at__gt=timezone.now()
    ).first()


@transaction.atomic
def complete_signup(*, token):
    """Con el correo verificado crea usuario + organización (plan Prueba, 30 días) +
    membresía de administrador en una sola transacción (ORG-02 CA1). El enlace sirve una vez."""
    pending = (
        PendingSignup.objects.select_for_update()
        .filter(token_hash=hash_token(token), completed_at__isnull=True, expires_at__gt=timezone.now())
        .first()
    )
    if pending is None:
        raise SignupError(_("El enlace no es válido, ya se usó o venció. Regístrate de nuevo."))
    if User.objects.filter(email__iexact=pending.email).exists():
        raise SignupError(_("Ya existe una cuenta con este correo: ingresa con tu contraseña."))
    trial = Plan.objects.filter(code="trial").first()
    if trial is None:
        ensure_default_plans(Plan)
        trial = Plan.objects.get(code="trial")
    now = timezone.now()
    organization = Organization.objects.create(
        name=pending.organization_name,
        slug=_unique_slug(pending.organization_name),
        plan=trial,
        status=Organization.Status.TRIAL,
        trial_ends_at=now + datetime.timedelta(days=TRIAL_DAYS),
    )
    user = create_user(
        email=pending.email, full_name=pending.full_name, email_verified=True, last_organization=organization
    )
    user.password = pending.password_hash
    user.save(update_fields=["password"])
    membership = Membership.objects.create(user=user, organization=organization, role=Membership.Role.ADMIN)
    pending.completed_at = now
    pending.password_hash = ""
    pending.save(update_fields=["completed_at", "password_hash", "updated_at"])
    with tenant_context(organization):
        audit.record(action=AuditEvent.Action.CREATE, target=organization, membership=membership)
    return user, organization
