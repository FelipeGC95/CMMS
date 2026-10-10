"""Indicadores básicos (KPI-01). Definiciones en docs/02_requisitos.md."""

import copy
import datetime
from dataclasses import dataclass

from django.db.models import Q
from django.utils import timezone

from apps.accounts.scoping import allowed_location_ids
from apps.assets.models import Asset, AssetStatusPeriod
from apps.assets.selectors import asset_scope_ids, visible_assets
from apps.maintenance.models import PlanAssignment
from apps.maintenance.selectors import is_on_time
from apps.organizations.selectors import descendant_ids
from apps.work.models import FailureEvent, WorkOrder
from apps.work.selectors import OPEN_STATUSES, in_locations, visible_work_orders

DOWN_STATUSES = (Asset.Status.STOPPED, Asset.Status.IN_MAINTENANCE)


@dataclass
class Kpis:
    availability_pct: float | None
    mttr_hours: float | None
    mtbf_hours: float | None
    preventive_compliance_pct: float | None
    failures: int
    open_work_orders: int
    overdue_work_orders: int
    stopped_assets: int
    upcoming_preventives: int
    # Hay activos en el alcance, pero todos estuvieron de baja toda la ventana: sin
    # tiempo en servicio, disponibilidad y MTBF no se pueden calcular.
    no_service_time: bool = False
    # Días (en la zona activa) que abarca la ventana medida.
    period_start: datetime.date | None = None
    period_end: datetime.date | None = None


def _overlap_seconds(start, end, window_start, window_end) -> float:
    start, end = max(start, window_start), min(end, window_end)
    return max((end - start).total_seconds(), 0.0)


def _by_asset_scope(qs, location_ids):
    """Limita a los activos de las ubicaciones permitidas (None = sin límite)."""
    return qs if location_ids is None else qs.filter(asset__location_id__in=location_ids)


@dataclass
class Reliability:
    availability_pct: float | None
    mttr_hours: float | None
    mtbf_hours: float | None
    failures: int
    # Horas en servicio (no de baja) en la ventana, sumadas sobre los activos.
    calendar_hours: float = 0.0
    asset_count: int = 0


def _retired_at(intervals, at) -> bool:
    return any(start <= at < end for start, end in intervals)


def _reliability(*, assets, failures, window_start, now) -> Reliability:
    """Disponibilidad, MTTR y MTBF (KPI-01) de `assets` en la ventana.

    Regla para activos dados de baja: el tiempo calendario de cada activo es
    el que pasó en la ventana fuera del estado "de baja" (un activo dado de
    baja antes de la ventana aporta 0 h; uno dado de baja dentro, solo hasta la
    baja). Las fallas iniciadas mientras el activo o componente estaba de baja
    no cuentan. Sin tiempo calendario, disponibilidad y MTBF quedan en None.

    `assets`: activos a medir (incluidos los de baja); `failures`: fallas
    candidatas (solo cuentan las de detención total iniciadas en la ventana).
    """
    window_seconds = (now - window_start).total_seconds()
    statuses = dict(assets.values_list("pk", "status"))
    periods = (
        AssetStatusPeriod.objects.filter(asset_id__in=assets.values("pk"), started_at__lt=now)
        .exclude(ended_at__lt=window_start)
        .only("asset_id", "status", "started_at", "ended_at")
    )
    downtime = 0.0
    retired = {}
    for p in periods:
        end = p.ended_at or now
        if p.status in DOWN_STATUSES:
            downtime += _overlap_seconds(p.started_at, end, window_start, now)
        elif p.status == Asset.Status.RETIRED:
            retired.setdefault(p.asset_id, []).append((max(p.started_at, window_start), min(end, now)))
    # Activo de baja sin período registrado (datos anteriores a AST-04): de baja toda la ventana.
    for pk, status in statuses.items():
        if status == Asset.Status.RETIRED and pk not in retired:
            retired[pk] = [(window_start, now)]
    retired_seconds = sum(
        (end - start).total_seconds() for spans in retired.values() for start, end in spans if end > start
    )
    calendar = len(statuses) * window_seconds - retired_seconds
    availability = 100 * (1 - downtime / calendar) if calendar > 0 else None

    failures = [
        f
        for f in failures.filter(impact=FailureEvent.Impact.TOTAL_STOP, started_at__gte=window_start).only(
            "asset_id", "component_id", "started_at", "ended_at"
        )
        if not any(_retired_at(retired.get(pk, ()), f.started_at) for pk in (f.asset_id, f.component_id))
    ]
    repaired = [f for f in failures if f.ended_at]
    mttr = (
        sum((f.ended_at - f.started_at).total_seconds() for f in repaired) / len(repaired) / 3600
        if repaired
        else None
    )
    operating = max(calendar - downtime, 0.0)
    mtbf = operating / len(failures) / 3600 if failures and calendar > 0 else None
    return Reliability(availability, mttr, mtbf, len(failures), max(calendar, 0.0) / 3600, len(statuses))


def asset_kpis(*, membership, asset, include_components: bool = False, days: int = 30, now=None):
    """Disponibilidad, MTBF y MTTR de un activo y, opcionalmente, de sus componentes (AST-03 CA2).

    Se calculan sobre el conjunto (activo + componentes visibles) como si
    fuera la flota del tablero: la detención de cada uno descuenta de su propio
    tiempo calendario. Una falla cuenta si el activo o el componente afectado
    está en el conjunto. Los períodos de baja no suman tiempo calendario (ver
    `_reliability`).
    """
    now = now or timezone.now()
    window_start = now - datetime.timedelta(days=days)
    ids = asset_scope_ids(membership=membership, asset=asset, include_components=include_components)
    return _reliability(
        assets=visible_assets(membership=membership).filter(pk__in=ids, is_active=True),
        failures=FailureEvent.objects.filter(Q(asset_id__in=ids) | Q(component_id__in=ids)).distinct(),
        window_start=window_start,
        now=now,
    )


@dataclass(frozen=True)
class KpiFilters:
    """Filtros del tablero (KPI-01): ventana y conjunto de activos.

    Sin `date_from`/`date_to` la ventana son los últimos `days` días; con ellos,
    del inicio de `date_from` al final de `date_to` (en la zona activa), sin
    pasar de ahora. `location` incluye sus descendientes.
    """

    days: int = 30
    date_from: datetime.date | None = None
    date_to: datetime.date | None = None
    location: object = None
    asset_class: object = None
    criticality: str = ""
    asset: object = None

    @property
    def narrows_assets(self) -> bool:
        return bool(self.asset_class or self.criticality or self.asset)

    @property
    def any(self) -> bool:
        return self.narrows_assets or self.location is not None

    def window(self, now):
        """(inicio, término) de la ventana como instantes; el término nunca pasa de `now`."""
        if self.date_to is not None:
            end = min(_start_of(self.date_to + datetime.timedelta(days=1)), now)
        else:
            end = now
        if self.date_from is not None:
            start = _start_of(self.date_from)
        else:
            start = end - datetime.timedelta(days=self.days)
        return min(start, end), end


def _start_of(day: datetime.date):
    return timezone.make_aware(datetime.datetime.combine(day, datetime.time.min))


def filtered_assets(*, membership, filters: KpiFilters, location_ids=None):
    """Activos vigentes y visibles que cumplen los filtros del tablero."""
    qs = visible_assets(membership=membership).filter(is_active=True)
    if location_ids is not None:
        qs = qs.filter(location_id__in=location_ids)
    if filters.asset_class is not None:
        qs = qs.filter(asset_class=filters.asset_class)
    if filters.criticality:
        qs = qs.filter(criticality=filters.criticality)
    if filters.asset is not None:
        qs = qs.filter(pk=filters.asset.pk)
    return qs


@dataclass(frozen=True)
class KpiScope:
    """Conjuntos que miden los indicadores con los filtros y el alcance de la membresía."""

    assets: object  # activos vigentes que cumplen los filtros
    failures: object  # fallas de esos activos (sin filtros: las del alcance)
    work_orders: object  # OT visibles de esos activos o, con solo sitio, de esas ubicaciones


def kpi_scope(*, membership, filters: KpiFilters) -> KpiScope:
    """Alcance común del tablero (KPI-01) y de los indicadores ampliados (KPI-02).

    Sin filtros: todo lo visible para la membresía (ORG-06). Con filtros de activo
    (clase, criticidad, activo), las OT y fallas de esos activos; con solo sitio,
    las OT de esa ubicación y sus descendientes (incluidas las OT sobre ubicaciones).
    """
    filter_location_ids = (
        {filters.location.pk, *descendant_ids(filters.location)} if filters.location is not None else None
    )
    assets = filtered_assets(membership=membership, filters=filters, location_ids=filter_location_ids)
    failures = _by_asset_scope(FailureEvent.objects, allowed_location_ids(membership))
    work_orders = visible_work_orders(membership=membership)
    if filters.any:
        failures = FailureEvent.objects.filter(asset__in=assets)
        if filters.narrows_assets:
            work_orders = work_orders.filter(asset__in=assets)
        else:
            work_orders = work_orders.filter(in_locations(filter_location_ids))
    return KpiScope(assets=assets, failures=failures, work_orders=work_orders)


def _compliance_outcome(work_order, now) -> bool | None:
    """¿La OT preventiva vencida cuenta como cumplida (True), incumplida (False) o no cuenta (None)?

    Misma regla que `apps.maintenance.selectors.is_on_time` (PM-02 CA3): vencimiento ±
    `tolerance_pct` de la versión del plan, por fecha o por medidor (basta uno). Sin versión
    de plan (preventiva manual) la tolerancia es cero: a tiempo si se completó a más tardar
    en su fecha límite. Una OT abierta cuya ventana de tolerancia aún no termina no cuenta
    todavía (None, fuera del denominador); si ya terminó, es incumplida. Espera la OT con
    `plan_version` precargada: no consulta la base.
    """
    if work_order.completed_at is None:
        if work_order.plan_version_id is None:
            return False
        probe = copy.copy(work_order)
        probe.completed_at = now
        probe.meter_value_at_completion = None
        # Completarla ahora aún caería dentro de la tolerancia: pendiente, no incumplida.
        return None if is_on_time(probe) else False
    on_time = is_on_time(work_order)
    if on_time is None:
        return timezone.localdate(work_order.completed_at) <= work_order.due_on
    return on_time


def dashboard_kpis(*, membership, days: int = 30, now=None, filters: KpiFilters | None = None) -> Kpis:
    """KPIs de la organización o, si la membresía tiene alcance, solo de sus sitios (ORG-06).

    `filters` acota la ventana y los activos (KPI-01); sin él, últimos `days` días.
    Los contadores del momento (OT abiertas, equipos detenidos, próximos
    preventivos) respetan los filtros de activos, no la ventana.
    """
    now = now or timezone.now()
    filters = filters or KpiFilters(days=days)
    window_start, window_end = filters.window(now)
    today = timezone.localdate(now)
    scope = kpi_scope(membership=membership, filters=filters)
    assets, failures, work_orders = scope.assets, scope.failures, scope.work_orders
    assignments = _by_asset_scope(PlanAssignment.objects, allowed_location_ids(membership))
    if filters.any:
        assignments = PlanAssignment.objects.filter(asset__in=assets)
    reliability = _reliability(assets=assets, failures=failures, window_start=window_start, now=window_end)

    # Preventivas vencidas en la ventana: fecha límite ya pasada (antes de hoy) y dentro del período.
    due_before = today
    if filters.date_to is not None:
        due_before = min(today, filters.date_to + datetime.timedelta(days=1))
    due_preventives = work_orders.filter(
        work_type=WorkOrder.Type.PREVENTIVE,
        due_on__gte=window_start.date(),
        due_on__lt=due_before,
    ).exclude(status=WorkOrder.Status.CANCELLED)
    # Una sola consulta: la versión del plan viene con la OT y la regla no consulta más.
    outcomes = [
        _compliance_outcome(wo, now)
        for wo in due_preventives.select_related("plan_version").only(
            "work_type",
            "due_on",
            "completed_at",
            "due_meter_value",
            "meter_value_at_completion",
            "plan_version__tolerance_pct",
            "plan_version__calendar_interval",
            "plan_version__calendar_unit",
            "plan_version__meter_interval",
        )
    ]
    measured = [outcome for outcome in outcomes if outcome is not None]
    compliance = 100 * sum(measured) / len(measured) if measured else None

    open_qs = work_orders.filter(status__in=OPEN_STATUSES)
    return Kpis(
        availability_pct=reliability.availability_pct,
        mttr_hours=reliability.mttr_hours,
        mtbf_hours=reliability.mtbf_hours,
        preventive_compliance_pct=compliance,
        failures=reliability.failures,
        open_work_orders=open_qs.count(),
        overdue_work_orders=open_qs.filter(due_on__lt=today).count(),
        stopped_assets=assets.filter(status__in=DOWN_STATUSES).count(),
        upcoming_preventives=assignments.filter(
            is_active=True, next_due_on__lte=today + datetime.timedelta(days=7)
        ).count(),
        no_service_time=reliability.asset_count > 0 and reliability.calendar_hours == 0,
        period_start=timezone.localdate(window_start),
        period_end=timezone.localdate(window_end - datetime.timedelta(microseconds=1)),
    )
