"""Lecturas de planes preventivos."""

from django.db.models import Count, Prefetch, Q

from apps.accounts.permissions import Perm, require_perm
from apps.assets.models import Meter
from apps.assets.selectors import visible_assets

from .models import MaintenancePlan, MaintenanceSettings, PlanAssignment, PlanVersion

TRIGGERS = ("calendar", "meter", "mixed")


def plan_list(*, membership, search: str = "", active: str = "", asset_class=None, trigger: str = ""):
    """Planes de la organización con su versión vigente y cuántos activos los tienen.

    `active`: "1" activos, "0" inactivos, "" todos. `trigger`: calendar, meter o mixed
    (según la versión vigente).
    """
    require_perm(membership, Perm.PLAN_MANAGE)
    qs = (
        MaintenancePlan.objects.select_related("current_version", "asset_class")
        .annotate(
            assignment_count=Count("assignments", filter=Q(assignments__is_active=True), distinct=True),
            draft_count=Count("versions", filter=Q(versions__status=PlanVersion.Status.DRAFT), distinct=True),
        )
        .order_by("name")
    )
    if search:
        qs = qs.filter(Q(code__icontains=search) | Q(name__icontains=search))
    if active in ("0", "1"):
        qs = qs.filter(is_active=active == "1")
    if asset_class is not None:
        qs = qs.filter(asset_class=asset_class)
    has_days = Q(current_version__calendar_interval__isnull=False)
    has_meter = Q(current_version__meter_interval__isnull=False) & ~Q(current_version__meter_kind="")
    if trigger == "calendar":
        qs = qs.filter(has_days).exclude(has_meter)
    elif trigger == "meter":
        qs = qs.filter(has_meter).exclude(has_days)
    elif trigger == "mixed":
        qs = qs.filter(has_days & has_meter)
    return qs


def plan_versions(*, plan):
    """Historial de versiones (la más reciente primero) con quién publicó."""
    return plan.versions.select_related("published_by").order_by("-number")


def version_content(version):
    """Checklist y recursos de una versión (dos consultas)."""
    if version is None:
        return [], []
    return list(version.items.order_by("order")), list(version.resources.all())


def plan_assignments(*, membership, plan):
    """Asignaciones activas del plan sobre activos del alcance de la membresía."""
    return (
        PlanAssignment.objects.filter(
            plan=plan, is_active=True, asset__in=visible_assets(membership=membership)
        )
        .select_related("asset__location", "meter")
        .order_by("asset__code")
    )


def assignable_assets(*, membership, plan, asset_class=None, location_ids=None, search: str = ""):
    """Activos del alcance que aún no tienen el plan activo, con sus medidores del tipo del plan."""
    qs = (
        visible_assets(membership=membership)
        .filter(is_active=True)
        .exclude(status="retired")
        .exclude(pk__in=PlanAssignment.objects.filter(plan=plan, is_active=True).values("asset_id"))
        .select_related("asset_class", "location")
        .order_by("code")
    )
    if asset_class is not None:
        qs = qs.filter(asset_class=asset_class)
    if location_ids is not None:
        qs = qs.filter(location_id__in=location_ids)
    if search:
        qs = qs.filter(Q(code__icontains=search) | Q(name__icontains=search))
    if plan.uses_meter:
        qs = qs.prefetch_related(
            Prefetch(
                "meters",
                queryset=Meter.objects.filter(kind=plan.meter_kind, is_active=True).order_by("name"),
                to_attr="plan_meters",
            )
        )
    return qs


def assignable_plans(*, asset):
    """Planes activos con versión publicada que el activo aún no tiene."""
    return (
        MaintenancePlan.objects.filter(is_active=True, current_version__isnull=False)
        .exclude(pk__in=PlanAssignment.objects.filter(asset=asset, is_active=True).values("plan_id"))
        .order_by("name")
    )


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


def maintenance_settings() -> MaintenanceSettings:
    """Ajustes de la organización del contexto; sin fila guardada, los valores por defecto."""
    return MaintenanceSettings.objects.first() or MaintenanceSettings()
