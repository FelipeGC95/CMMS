import hashlib

from django.db.models import Count, Q
from django.utils import timezone

from .models import CustomRole, Invitation, Membership
from .permissions import CUSTOM_ROLES_MODULE, ROLE_PERMISSIONS, Perm, require_perm


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def pending_invitations_qs(organization):
    return Invitation.objects.filter(
        organization=organization,
        accepted_at__isnull=True,
        revoked_at__isnull=True,
        expires_at__gt=timezone.now(),
    )


def team_members(*, membership):
    """Membresías de la organización activa, activas primero."""
    return (
        Membership.objects.filter(organization=membership.organization)
        .select_related("user", "custom_role")
        .prefetch_related("scope__location")
        .order_by("-is_active", "user__full_name", "user__email")
    )


def team_member(*, membership, pk) -> Membership:
    """Una membresía de la organización activa; nunca de otra (lanza DoesNotExist)."""
    return (
        Membership.objects.select_related("user", "custom_role")
        .prefetch_related("scope")
        .get(organization=membership.organization, pk=pk)
    )


def pending_invitations(*, membership):
    return (
        pending_invitations_qs(membership.organization)
        .select_related("invited_by", "custom_role")
        .order_by("-created_at")
    )


def invitation_by_token(token: str) -> Invitation | None:
    """Invitación vigente del enlace (ORG-05); None si no existe, venció, se usó o se revocó."""
    return (
        Invitation.objects.select_related("organization")
        .filter(
            token_hash=hash_token(token),
            accepted_at__isnull=True,
            revoked_at__isnull=True,
            expires_at__gt=timezone.now(),
            organization__is_active=True,
        )
        .first()
    )


# ---------- Roles personalizados (ORG-12, ADR-031) ----------


def custom_role_list(*, membership):
    """Roles personalizados de la organización activa con cuántas personas activas los tienen."""
    require_perm(membership, Perm.TEAM_MANAGE)
    return CustomRole.objects.annotate(
        member_count=Count("memberships", filter=Q(memberships__is_active=True))
    ).order_by("-is_active", "name")


def custom_role_detail(*, membership, pk) -> CustomRole:
    """Un rol de la organización activa; nunca de otra (lanza DoesNotExist)."""
    require_perm(membership, Perm.TEAM_MANAGE)
    return CustomRole.objects.get(pk=pk)


def assignable_custom_roles(*, membership) -> list:
    """Roles personalizados activos para el selector de rol (vacío si el plan no tiene el módulo)."""
    from apps.organizations.modules import has_module

    if not has_module(membership.organization, CUSTOM_ROLES_MODULE):
        return []
    return list(CustomRole.objects.filter(is_active=True).order_by("name"))


def perm_holders_q(*, organization, perm: Perm) -> Q:
    """Filtro sobre Membership: quienes tienen `perm` vigente, por rol base o personalizado.

    Requiere el contexto de tenant. Lee los roles personalizados activos (pocos) y decide en
    Python: así no depende de consultas JSON propias de PostgreSQL.
    """
    from apps.organizations.modules import has_module

    by_base = Q(role__in=[role for role, perms in ROLE_PERMISSIONS.items() if perm in perms])
    if not has_module(organization, CUSTOM_ROLES_MODULE):
        return by_base
    granting, denying = [], []
    for pk, codes in CustomRole.objects.filter(is_active=True).values_list("pk", "permissions"):
        (granting if perm.value in codes else denying).append(pk)
    return (by_base & ~Q(custom_role__in=denying)) | Q(custom_role__in=granting)
