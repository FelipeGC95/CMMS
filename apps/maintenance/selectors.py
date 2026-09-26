"""Lecturas de planes preventivos."""

from .models import PlanAssignment


def active_assignments_for_asset(*, asset):
    """Asignaciones de plan activas del activo, con plan y medidor (sin N+1)."""
    return (
        PlanAssignment.objects.filter(asset=asset, is_active=True)
        .select_related("plan__current_version", "meter")
        .order_by("plan__name")
    )
