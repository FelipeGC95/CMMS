"""Lecturas de planes preventivos."""

import datetime
import math
from dataclasses import dataclass
from decimal import Decimal

from django.db.models import Count, Prefetch, Q
from django.utils import timezone

from apps.accounts.permissions import Perm, require_perm
from apps.assets.models import Meter
from apps.assets.selectors import visible_assets
from apps.organizations.selectors import descendant_ids

from .models import (
    MaintenancePlan,
    MaintenanceSettings,
    PlanAbsorption,
    PlanAssignment,
    PlanVersion,
    add_months,
)
from .projection import MAX_OCCURRENCES, Occurrence, project

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
    return list(version.items.order_by("order")), list(version.resources.select_related("part"))


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


def plan_absorptions(*, plan):
    """Planes que `plan` absorbe y candidatos para agregar (activos, sin él mismo ni los ya elegidos)."""
    absorptions = list(
        plan.absorptions.select_related("absorbed_plan__current_version").order_by("absorbed_plan__name")
    )
    taken = {a.absorbed_plan_id for a in absorptions} | {plan.pk}
    choices = MaintenancePlan.objects.filter(is_active=True).exclude(pk__in=taken).order_by("name")
    return absorptions, choices


def is_on_time(work_order) -> bool | None:
    """¿La OT preventiva se completó dentro de su vencimiento ± tolerancia? (PM-02 CA3)

    La tolerancia es `tolerance_pct` de la versión con que se generó la OT,
    aplicada a su frecuencia: por calendario, en días del ciclo que vence en
    `due_on` (redondeo hacia abajo); por medidor, en unidades sobre
    `due_meter_value` con la lectura de cierre. Con los dos disparadores basta
    cumplir uno (vence lo que ocurra primero). Devuelve None si no aplica: no
    es preventiva, no está completada o no hay con qué medir. Para indicadores.
    """
    version = work_order.plan_version
    if work_order.work_type != "preventive" or work_order.completed_at is None or version is None:
        return None
    pct = Decimal(version.tolerance_pct) / 100
    checks = []
    if work_order.due_on is not None:
        period = version.calendar_period_days(work_order.due_on) if version.calendar_interval else 0
        window = datetime.timedelta(days=math.floor(period * pct))
        done = timezone.localdate(work_order.completed_at)
        checks.append(work_order.due_on - window <= done <= work_order.due_on + window)
    if (
        work_order.due_meter_value is not None
        and work_order.meter_value_at_completion is not None
        and version.meter_interval
    ):
        window = version.meter_interval * pct
        checks.append(abs(work_order.meter_value_at_completion - work_order.due_meter_value) <= window)
    return any(checks) if checks else None


def busy_assignment_ids(assignments) -> set:
    """Asignaciones con una OT abierta, propia o de un plan que las absorbió (PM-05 CA2/CA3)."""
    from apps.work.models import WorkOrder, WorkOrderAbsorption

    own = WorkOrder.objects.filter(plan_assignment__in=assignments).exclude(
        status__in=WorkOrder.CLOSED_STATUSES
    )
    absorbed = WorkOrderAbsorption.objects.filter(plan_assignment__in=assignments).exclude(
        work_order__status__in=WorkOrder.CLOSED_STATUSES
    )
    return set(own.values_list("plan_assignment_id", flat=True)) | set(
        absorbed.values_list("plan_assignment_id", flat=True)
    )


# --- Calendario de preventivos (PM-06) ----------------------------------------

# Quién ve el calendario: los roles que ejecutan o gestionan OT (administrador,
# planificador, supervisor y técnico), cada uno dentro de su alcance por sitio.
CALENDAR_PERM = Perm.WORK_ORDER_EXECUTE
# OT existentes que se muestran junto a la proyección: las por hacer y las que
# ya empezaron (en ejecución o en espera), para no perderlas de vista.
CALENDAR_WO_STATUSES = ("planned", "scheduled", "in_progress", "on_hold")
# OT ya iniciadas: el calendario las distingue con su estado (ícono + texto).
CALENDAR_STARTED_STATUSES = ("in_progress", "on_hold")


@dataclass
class PlanCalendar:
    start: datetime.date
    end: datetime.date
    # Proyectadas y OT existentes, por fecha (las absorbidas llevan `absorbed_into`).
    occurrences: list
    # Asignaciones por medidor sin uso promedio: no tienen fecha proyectable.
    unestimated: list
    truncated: bool


def plan_calendar(
    *,
    membership,
    start: datetime.date,
    end: datetime.date,
    location=None,
    asset_class=None,
    asset_q: str = "",
    plan=None,
    today: datetime.date | None = None,
) -> PlanCalendar:
    """Preventivos entre `start` y `end`: OT abiertas (por hacer o iniciadas) y la proyección (PM-06 CA1).

    Consultas acotadas (no crecen con las asignaciones): asignaciones de los
    activos visibles filtrados, reglas de absorción, OT abiertas de esas
    asignaciones y OT del período. Con `plan`, se proyectan todos los planes
    de los activos que lo tienen (para respetar las absorciones) y se muestran
    solo las ocurrencias de ese plan.
    """
    from apps.work.selectors import in_locations, visible_work_orders

    require_perm(membership, CALENDAR_PERM)
    today = today or timezone.localdate()
    location_ids = {location.pk, *descendant_ids(location)} if location is not None else None
    assets = visible_assets(membership=membership).filter(is_active=True).exclude(status="retired")
    if location_ids is not None:
        assets = assets.filter(location_id__in=location_ids)
    if asset_class is not None:
        assets = assets.filter(asset_class=asset_class)
    if asset_q:
        assets = assets.filter(Q(code__icontains=asset_q) | Q(name__icontains=asset_q))
    if plan is not None:
        assets = assets.filter(plan_assignments__plan=plan, plan_assignments__is_active=True)

    occurrences, unestimated, truncated = [], [], False
    if end >= today:
        assignments = list(
            PlanAssignment.objects.filter(
                is_active=True,
                plan__is_active=True,
                plan__current_version__isnull=False,
                asset__in=assets.values("pk"),
            ).select_related("plan__current_version", "asset__location", "meter")
        )
        if assignments:
            absorptions = {}
            for plan_id, absorbed_id in PlanAbsorption.objects.filter(
                absorbed_plan__is_active=True
            ).values_list("plan_id", "absorbed_plan_id"):
                absorptions.setdefault(plan_id, set()).add(absorbed_id)
            occurrences, unestimated, truncated = project(
                assignments=assignments,
                absorptions=absorptions,
                busy=busy_assignment_ids([a.pk for a in assignments]),
                today=today,
                end=end,
            )
        occurrences = [o for o in occurrences if o.date >= start and (plan is None or o.plan.pk == plan.pk)]
        unestimated = [a for a in unestimated if plan is None or a.plan_id == plan.pk]

    orders = (
        visible_work_orders(membership=membership)
        .filter(status__in=CALENDAR_WO_STATUSES, due_on__gte=start, due_on__lte=end)
        .select_related("asset__location", "location", "plan_assignment__plan")
    )
    if asset_class is not None or asset_q or plan is not None:
        orders = orders.filter(asset__in=assets.values("pk"))
    elif location_ids is not None:
        orders = orders.filter(in_locations(location_ids))
    if plan is not None:
        orders = orders.filter(plan_assignment__plan=plan)
    for order in orders.order_by("due_on", "number")[:MAX_OCCURRENCES]:
        assignment = order.plan_assignment
        occurrences.append(
            Occurrence(
                date=order.due_on,
                plan=assignment.plan if assignment is not None else None,
                asset=order.asset,
                work_order=order,
            )
        )
    occurrences.sort(key=lambda o: (o.date, o.work_order is None, o.absorbed_into is not None))
    return PlanCalendar(start, end, occurrences, unestimated, truncated)


def calendar_months(calendar: PlanCalendar) -> list[dict]:
    """Resumen por mes (vista «12 meses»): OT previstas, absorbidas y minutos estimados."""
    months = {}
    cursor = calendar.start.replace(day=1)
    while cursor <= calendar.end:
        months[(cursor.year, cursor.month)] = {"month": cursor, "count": 0, "absorbed": 0, "minutes": 0}
        cursor = add_months(cursor, 1)
    for occurrence in calendar.occurrences:
        row = months.get((occurrence.date.year, occurrence.date.month))
        if row is None:
            continue
        if occurrence.absorbed_into is not None:
            row["absorbed"] += 1
        else:
            row["count"] += 1
            row["minutes"] += occurrence.estimated_minutes
    return list(months.values())
