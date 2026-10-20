"""SSO por OIDC (ORG-11, ADR-032): configuración, dominios verificados por DNS e ingreso.

- Configurar y verificar dominios es solo para administradores (`organization.manage`) y
  pide el módulo `sso` del plan (Empresa).
- Un dominio se verifica con un registro TXT `_galbust-verification.<dominio>`; sin dominio
  verificado no hay ingreso por SSO, alta automática ni exigencia.
- El ingreso (`begin_login` / `complete_login`) no recibe membresía: corre antes de la
  sesión y fija el contexto de la organización dueña del dominio.
"""

import re
import secrets
import time
from dataclasses import dataclass

from allauth.account.models import EmailAddress
from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.translation import gettext as _

from apps.accounts.models import CUSTOM_ROLE_BASES, CustomRole, Membership, User
from apps.accounts.permissions import CUSTOM_ROLES_MODULE, Perm, require_perm
from apps.accounts.selectors import pending_invitations_qs
from apps.accounts.services import TeamError, accept_invitation, check_seat_available, create_user
from apps.audit import services as audit
from apps.audit.models import AuditEvent
from apps.core.tenancy import tenant_context
from apps.organizations.models import Organization
from apps.organizations.modules import ModuleNotIncludedError, has_module

from . import crypto, oidc
from .models import SsoConfiguration, SsoDomain
from .oidc import SsoError
from .selectors import MODULE, email_domain, sso_for_email

__all__ = ["SsoError"]

FLOW_TTL_SECONDS = 600  # el intento de ingreso vence a los 10 minutos
METADATA_TTL_SECONDS = 24 * 3600  # el descubrimiento se vuelve a leer una vez al día
AUDIT_FIELDS = {
    "discovery_url",
    "client_id",
    "default_role",
    "default_custom_role",
    "enforce_sso",
    "is_active",
}
DOMAIN_RE = re.compile(r"^(?=.{4,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
# Correo personal: nadie lo "posee" como empresa (defensa extra a la verificación por DNS).
PUBLIC_EMAIL_DOMAINS = frozenset(
    {
        "gmail.com",
        "googlemail.com",
        "outlook.com",
        "hotmail.com",
        "hotmail.es",
        "live.com",
        "live.cl",
        "msn.com",
        "yahoo.com",
        "yahoo.es",
        "icloud.com",
        "me.com",
        "aol.com",
        "proton.me",
        "protonmail.com",
        "gmx.com",
        "yandex.com",
        "mail.com",
    }
)


def _require_admin(membership):
    require_perm(membership, Perm.ORGANIZATION_MANAGE)
    if not has_module(membership.organization, MODULE):
        raise ModuleNotIncludedError(membership.organization, MODULE)


# --- Configuración ------------------------------------------------------------------------


def _clean_default_role(membership, default_role, default_custom_role):
    """Rol base (nunca administrador) o rol personalizado activo de la organización (ORG-12)."""
    if default_custom_role is not None:
        if not has_module(membership.organization, CUSTOM_ROLES_MODULE):
            raise ModuleNotIncludedError(membership.organization, CUSTOM_ROLES_MODULE)
        found = CustomRole.objects.filter(
            pk=getattr(default_custom_role, "pk", default_custom_role), is_active=True
        ).first()
        if found is None:
            raise SsoError(_("Rol no válido."))
        return found.base_role, found
    if default_role not in dict(CUSTOM_ROLE_BASES):
        raise SsoError(_("El rol por defecto no puede ser administrador."))
    return default_role, None


@transaction.atomic
def save_configuration(
    *,
    membership,
    discovery_url: str,
    client_id: str,
    client_secret: str = "",
    default_role: str,
    default_custom_role=None,
    enforce_sso: bool = False,
    is_active: bool = False,
) -> SsoConfiguration:
    """Guarda el proveedor OIDC. Valida el descubrimiento (https, sin SSRF) cada vez.

    `client_secret` vacío conserva el guardado; nunca se devuelve en claro.
    """
    _require_admin(membership)
    discovery_url = oidc.normalize_discovery_url(discovery_url)
    client_id = client_id.strip()
    if not client_id:
        raise SsoError(_("Escribe el ID de cliente."))
    metadata = oidc.fetch_metadata(discovery_url)
    role, custom_role = _clean_default_role(membership, default_role, default_custom_role)
    if enforce_sso and not is_active:
        raise SsoError(_("Para exigir SSO, primero actívalo."))

    configuration = SsoConfiguration.objects.select_for_update().first()
    created = configuration is None
    if created:
        configuration = SsoConfiguration()
    before = audit.snapshot(configuration, AUDIT_FIELDS)
    if client_secret.strip():
        configuration.client_secret_encrypted = crypto.encrypt(client_secret.strip())
    elif not configuration.has_secret:
        raise SsoError(_("Escribe el secreto del cliente."))
    configuration.discovery_url = discovery_url
    configuration.client_id = client_id
    configuration.default_role = role
    configuration.default_custom_role = custom_role
    configuration.enforce_sso = enforce_sso
    configuration.is_active = is_active
    configuration.provider_metadata = metadata
    configuration.metadata_fetched_at = timezone.now()
    configuration.save()
    changes = audit.diff(before, audit.snapshot(configuration, AUDIT_FIELDS))
    if client_secret.strip():
        changes["credentials"] = [None, "updated"]
    if created or changes:
        audit.record(
            action=AuditEvent.Action.CREATE if created else AuditEvent.Action.UPDATE,
            target=configuration,
            membership=membership,
            changes=changes,
        )
    return configuration


def test_connection(*, membership) -> SsoConfiguration:
    """Lee el descubrimiento y las claves del proveedor y revisa el secreto guardado.

    Guarda el resultado (correcto o con el error) para mostrarlo en la pantalla.
    """
    _require_admin(membership)
    configuration = SsoConfiguration.objects.first()
    if configuration is None:
        raise SsoError(_("Primero guarda la configuración del proveedor."))
    error = ""
    try:
        metadata = oidc.fetch_metadata(configuration.discovery_url)
        oidc.fetch_jwks(metadata)
        crypto.decrypt(configuration.client_secret_encrypted)
    except SsoError as failure:
        error = failure.message
    except crypto.SecretUnavailableError:
        error = _("El secreto guardado no se puede leer: vuelve a escribirlo.")
    else:
        configuration.provider_metadata = metadata
        configuration.metadata_fetched_at = timezone.now()
    configuration.last_tested_at = timezone.now()
    configuration.last_test_ok = not error
    configuration.last_test_error = error[:300]
    configuration.save()
    return configuration


# --- Dominios -----------------------------------------------------------------------------


def lookup_txt(name: str) -> list[str]:
    """Registros TXT del nombre (los tests reemplazan esta función: sin DNS real)."""
    import dns.exception
    import dns.resolver

    try:
        answer = dns.resolver.resolve(name, "TXT", lifetime=5)
    except dns.exception.DNSException:
        return []
    return [b"".join(record.strings).decode("utf-8", "replace") for record in answer]


def _clean_domain(value: str) -> str:
    domain = (value or "").strip().lower().rstrip(".")
    if "@" in domain:
        domain = email_domain(domain)
    try:
        domain = domain.encode("idna").decode("ascii")
    except UnicodeError as error:
        raise SsoError(_("Escribe un dominio válido, p. ej. empresa.cl.")) from error
    if not DOMAIN_RE.match(domain):
        raise SsoError(_("Escribe un dominio válido, p. ej. empresa.cl."))
    if domain in PUBLIC_EMAIL_DOMAINS:
        raise SsoError(_("No se puede usar un dominio de correo personal."))
    return domain


def _verified_elsewhere(domain: str, organization_id) -> bool:
    # all_tenants: la unicidad del dominio verificado es de toda la plataforma (ORG-11 CA3).
    return (
        SsoDomain.all_tenants.filter(domain=domain, verified_at__isnull=False)
        .exclude(organization_id=organization_id)
        .exists()
    )


@transaction.atomic
def add_domain(*, membership, domain: str) -> SsoDomain:
    _require_admin(membership)
    domain = _clean_domain(domain)
    if SsoDomain.objects.filter(domain=domain).exists():
        raise SsoError(_("El dominio %(domain)s ya está en la lista.") % {"domain": domain})
    if _verified_elsewhere(domain, membership.organization_id):
        raise SsoError(_("El dominio %(domain)s ya pertenece a otra organización.") % {"domain": domain})
    sso_domain = SsoDomain.objects.create(domain=domain, verification_token=secrets.token_urlsafe(24))
    audit.record(
        action=AuditEvent.Action.CREATE, target=sso_domain, membership=membership, changes={"domain": domain}
    )
    return sso_domain


def verify_domain(*, membership, domain: SsoDomain) -> SsoDomain:
    """Busca el registro TXT. Si está, el dominio queda verificado (y es solo de esta organización)."""
    _require_admin(membership)
    if domain.is_verified:
        return domain
    found = domain.txt_value in lookup_txt(domain.txt_name)
    now = timezone.now()
    if not found:
        SsoDomain.objects.filter(pk=domain.pk).update(last_checked_at=now)
        raise SsoError(
            _(
                "Aún no encontramos el registro TXT en %(name)s. "
                "Los cambios de DNS pueden tardar unos minutos."
            )
            % {"name": domain.txt_name}
        )
    if _verified_elsewhere(domain.domain, domain.organization_id):
        raise SsoError(
            _("El dominio %(domain)s ya pertenece a otra organización.") % {"domain": domain.domain}
        )
    try:
        with transaction.atomic():
            domain.verified_at = now
            domain.last_checked_at = now
            domain.save(update_fields=["verified_at", "last_checked_at", "updated_at"])
            audit.record(
                action=AuditEvent.Action.UPDATE,
                target=domain,
                membership=membership,
                changes={"verified_at": [None, now.isoformat()]},
            )
    except IntegrityError as error:  # otra organización lo verificó al mismo tiempo
        domain.verified_at = None
        raise SsoError(
            _("El dominio %(domain)s ya pertenece a otra organización.") % {"domain": domain.domain}
        ) from error
    return domain


@transaction.atomic
def remove_domain(*, membership, domain: SsoDomain) -> None:
    """Quita el dominio (es configuración, no historial). Sus correos vuelven a la contraseña."""
    _require_admin(membership)
    audit.record(
        action=AuditEvent.Action.DELETE,
        target=domain,
        membership=membership,
        changes={"domain": domain.domain, "verified": domain.is_verified},
    )
    domain.delete()


# --- Ingreso ------------------------------------------------------------------------------


def _metadata(configuration: SsoConfiguration) -> dict:
    """Descubrimiento guardado; se vuelve a leer si tiene más de un día."""
    fetched = configuration.metadata_fetched_at
    if (
        configuration.provider_metadata
        and fetched
        and (timezone.now() - fetched).total_seconds() < (METADATA_TTL_SECONDS)
    ):
        return configuration.provider_metadata
    metadata = oidc.fetch_metadata(configuration.discovery_url)
    SsoConfiguration.objects.filter(pk=configuration.pk).update(
        provider_metadata=metadata, metadata_fetched_at=timezone.now()
    )
    return metadata


def begin_login(*, email: str, redirect_uri: str, next_url: str = "") -> tuple[str, dict]:
    """URL del IdP de la organización dueña del dominio y el intento a guardar en la sesión."""
    email = (email or "").strip().lower()
    target = sso_for_email(email)
    if target is None:
        raise SsoError(
            _("No hay inicio de sesión único para %(email)s. Entra con tu correo y contraseña.")
            % {"email": email}
        )
    with tenant_context(target.organization):
        metadata = _metadata(target.configuration)
    flow = {
        **oidc.new_flow(),
        "organization_id": str(target.organization.pk),
        "email": email,
        "next": next_url,
        "created_at": time.time(),
    }
    url = oidc.authorization_url(
        metadata=metadata,
        client_id=target.configuration.client_id,
        redirect_uri=redirect_uri,
        flow=flow,
        login_hint=email,
    )
    return url, flow


@dataclass(frozen=True)
class SsoLogin:
    user: User
    organization: Organization
    membership: Membership
    provisioned: bool  # se creó la membresía (alta automática o invitación aceptada)


def _check_flow(flow, params) -> None:
    if not flow:
        raise SsoError(_("El intento de inicio de sesión venció o ya se usó. Vuelve a intentarlo."))
    state = params.get("state") or ""
    if not secrets.compare_digest(str(state), str(flow.get("state", ""))):
        raise SsoError(
            _("La respuesta del proveedor no corresponde a este intento (state). Vuelve a intentarlo.")
        )
    if time.time() - float(flow.get("created_at", 0)) > FLOW_TTL_SECONDS:
        raise SsoError(_("El intento de inicio de sesión venció o ya se usó. Vuelve a intentarlo."))
    if params.get("error"):
        raise SsoError(
            _("El proveedor de identidad rechazó el ingreso (%(error)s).")
            % {"error": str(params["error"])[:80]}
        )
    if not params.get("code"):
        raise SsoError(_("El proveedor no entregó el código de autorización."))


def _claims_email(claims: dict) -> tuple[str, bool | None, str]:
    email = claims.get("email")
    verified = claims.get("email_verified")
    if isinstance(verified, str):  # algunos proveedores lo mandan como texto
        verified = verified.lower() == "true"
    return (email.strip().lower() if isinstance(email, str) else ""), verified, claims.get("name") or ""


def complete_login(*, flow: dict | None, params, redirect_uri: str) -> SsoLogin:
    """Valida la respuesta del IdP y devuelve a quién iniciar sesión (creándolo si corresponde).

    Garantías antes de enlazar con una cuenta por correo: el id_token es del IdP de la
    organización del intento (firma, emisor, audiencia, nonce), el correo es de un dominio
    verificado de esa misma organización y el proveedor no dice `email_verified: false`.
    """
    _check_flow(flow, params)
    organization = (
        Organization.objects.select_related("plan").filter(pk=flow["organization_id"], is_active=True).first()
    )
    if organization is None or not has_module(organization, MODULE):
        raise SsoError(_("El inicio de sesión único de esta organización no está disponible."))
    with tenant_context(organization):
        configuration = SsoConfiguration.objects.filter(is_active=True).first()
        if configuration is None:
            raise SsoError(_("El inicio de sesión único de esta organización no está disponible."))
        try:
            client_secret = crypto.decrypt(configuration.client_secret_encrypted)
        except crypto.SecretUnavailableError as error:
            raise SsoError(
                _("La configuración SSO de tu organización está incompleta. Avisa a su administrador.")
            ) from error
        metadata = _metadata(configuration)
        tokens = oidc.exchange_code(
            metadata=metadata,
            client_id=configuration.client_id,
            client_secret=client_secret,
            code=params["code"],
            redirect_uri=redirect_uri,
            code_verifier=flow["code_verifier"],
        )
        claims = oidc.validate_id_token(
            id_token=tokens["id_token"],
            jwks=oidc.fetch_jwks(metadata),
            issuer=metadata["issuer"],
            client_id=configuration.client_id,
            nonce=flow["nonce"],
        )
        email, email_verified, name = _claims_email(claims)
        if not email:
            info = oidc.fetch_userinfo(
                metadata=metadata, access_token=tokens.get("access_token", ""), subject=claims["sub"]
            )
            email, email_verified, name = _claims_email({**info, "name": name or info.get("name")})
        if not email:
            raise SsoError(
                _("El proveedor no entregó el correo. Pide a tu administrador incluir el dato «email».")
            )
        if email_verified is False:
            raise SsoError(
                _("El proveedor indica que el correo %(email)s no está verificado.") % {"email": email}
            )
        if not SsoDomain.objects.filter(domain=email_domain(email), verified_at__isnull=False).exists():
            raise SsoError(
                _("El correo %(email)s no es de un dominio verificado de %(organization)s.")
                % {"email": email, "organization": organization.name}
            )
        return _sign_in(organization=organization, configuration=configuration, email=email, name=name)


def _default_role(configuration: SsoConfiguration, organization) -> tuple[str, CustomRole | None]:
    """Rol de la alta automática. Un rol personalizado desactivado o sin módulo cae a su rol base."""
    custom = configuration.default_custom_role
    if custom is not None and custom.is_active and has_module(organization, CUSTOM_ROLES_MODULE):
        return custom.base_role, custom
    role = custom.base_role if custom is not None else configuration.default_role
    if role not in dict(CUSTOM_ROLE_BASES):  # nunca administrador, aunque se escriba en la base
        role = Membership.Role.TECHNICIAN
    return role, None


@transaction.atomic
def _sign_in(*, organization, configuration, email: str, name: str) -> SsoLogin:
    user = User.objects.filter(email__iexact=email).first()
    if user is not None and not user.is_active:
        raise SsoError(_("Tu cuenta está desactivada."))
    membership = Membership.objects.filter(user=user, organization=organization).first() if user else None
    if membership is not None and not membership.is_active:
        raise SsoError(
            _("Tu acceso a %(organization)s está desactivado. Habla con su administrador.")
            % {"organization": organization.name}
        )
    provisioned = False
    if membership is None:
        # Bloquea la organización: dos altas simultáneas no superan el cupo (ORG-11 CA5).
        Organization.objects.select_for_update().filter(pk=organization.pk).first()
        try:
            check_seat_available(organization, exclude_email=email)
        except TeamError as error:
            raise SsoError(
                _(
                    "%(organization)s llegó al máximo de usuarios de su plan. "
                    "Pide a su administrador liberar un cupo o cambiar de plan."
                )
                % {"organization": organization.name}
            ) from error
        if user is None:
            user = create_user(email=email, full_name=name[:150], email_verified=True)
        invitation = pending_invitations_qs(organization).filter(email=email).first()
        if invitation is not None:
            # Una invitación pendiente manda: entra con el rol con que lo invitaron.
            membership = accept_invitation(invitation=invitation, user=user)
        else:
            role, custom_role = _default_role(configuration, organization)
            membership = Membership.objects.create(
                user=user, organization=organization, role=role, custom_role=custom_role
            )
            audit.record(
                action=AuditEvent.Action.CREATE,
                target=membership,
                actor=user,
                changes={
                    "source": "sso",
                    "role": role,
                    **({"custom_role": custom_role.name} if custom_role else {}),
                },
            )
        provisioned = True
    # El IdP de un dominio verificado responde por el correo.
    EmailAddress.objects.filter(user=user, email__iexact=email).update(verified=True)
    if not EmailAddress.objects.filter(user=user, email__iexact=email).exists():
        EmailAddress.objects.create(user=user, email=email, primary=True, verified=True)
    User.objects.filter(pk=user.pk).update(last_organization=organization)
    user.last_organization = organization
    return SsoLogin(user=user, organization=organization, membership=membership, provisioned=provisioned)
