"""Lecturas de planes preventivos."""

from .models import PlanAssignment


def active_assignments_for_asset(*, asset):
    """Asignaciones de plan activas del activo, con plan y medidor (sin N+1)."""
    return (
        PlanAssignment.objects.filter(asset=asset, is_active=True)
        .select_related("plan__current_version", "meter")
        .order_by("plan__name")
    )


def reactivatable_assignments_for_asset(*, asset):
    """Asignaciones desactivadas (al dar de baja, AST-04 CA3) que se pueden volver a activar.

    Solo las de planes activos con versión publicada: son las que `assign_plan` acepta.
    """
    return (
        PlanAssignment.objects.filter(
            asset=asset, is_active=False, plan__is_active=True, plan__current_version__isnull=False
        )
        .select_related("plan__current_version", "meter")
        .order_by("plan__name")
    )
