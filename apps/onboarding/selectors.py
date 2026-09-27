"""Lecturas del asistente de puesta en marcha (ORG-03) para el inicio."""

from apps.accounts.permissions import Perm, has_perm

from .models import OnboardingState


def onboarding_for_dashboard(*, membership) -> OnboardingState | None:
    """El avance pendiente, solo para administradores; None si terminó, se descartó o no existe.

    Una consulta, y ninguna para los demás roles.
    """
    if membership is None or not has_perm(membership, Perm.ORGANIZATION_MANAGE):
        return None
    return OnboardingState.objects.filter(finished_at__isnull=True, dismissed_at__isnull=True).first()
