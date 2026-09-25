import hashlib

from django.utils import timezone

from .models import Invitation, Membership


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
        .select_related("user")
        .prefetch_related("scope__location")
        .order_by("-is_active", "user__full_name", "user__email")
    )


def team_member(*, membership, pk) -> Membership:
    """Una membresía de la organización activa; nunca de otra (lanza DoesNotExist)."""
    return (
        Membership.objects.select_related("user")
        .prefetch_related("scope")
        .get(organization=membership.organization, pk=pk)
    )


def pending_invitations(*, membership):
    return (
        pending_invitations_qs(membership.organization).select_related("invited_by").order_by("-created_at")
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
