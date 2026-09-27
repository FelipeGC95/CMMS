"""Indicadores básicos (KPI-01). Definiciones en docs/02_requisitos.md."""

import datetime
from dataclasses import dataclass

from django.db.models import Q
from django.utils import timezone

from apps.accounts.scoping import allowed_location_ids
from apps.assets.models import Asset, AssetStatusPeriod
from apps.assets.selectors import asset_scope_ids, visible_assets
from apps.maintenance.models import PlanAssignment
from apps.work.models import FailureEvent, WorkOrder
from apps.work.selectors import OPEN_STATUSES, visible_work_orders

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


def dashboard_kpis(*, membership, days: int = 30, now=None) -> Kpis:
    """KPIs de la organización o, si la membresía tiene alcance, solo de sus sitios (ORG-06)."""
    now = now or timezone.now()
    window_start = now - datetime.timedelta(days=days)
    today = timezone.localdate(now)
    location_ids = allowed_location_ids(membership)

    assets = visible_assets(membership=membership).filter(is_active=True)
    reliability = _reliability(
        assets=assets,
        failures=_by_asset_scope(FailureEvent.objects, location_ids),
        window_start=window_start,
        now=now,
    )

    due_preventives = (
        visible_work_orders(membership=membership)
        .filter(
            work_type=WorkOrder.Type.PREVENTIVE,
            due_on__gte=window_start.date(),
            due_on__lt=today,
        )
        .exclude(status=WorkOrder.Status.CANCELLED)
    )
    due_count = due_preventives.count()
    on_time = sum(
        1
        for wo in due_preventives.only("due_on", "completed_at")
        if wo.completed_at and timezone.localdate(wo.completed_at) <= wo.due_on
    )
    compliance = 100 * on_time / due_count if due_count else None

    open_qs = visible_work_orders(membership=membership).filter(status__in=OPEN_STATUSES)
    return Kpis(
        availability_pct=reliability.availability_pct,
        mttr_hours=reliability.mttr_hours,
        mtbf_hours=reliability.mtbf_hours,
        preventive_compliance_pct=compliance,
        failures=reliability.failures,
        open_work_orders=open_qs.count(),
        overdue_work_orders=open_qs.filter(due_on__lt=today).count(),
        stopped_assets=assets.filter(status__in=DOWN_STATUSES).count(),
        upcoming_preventives=_by_asset_scope(PlanAssignment.objects, location_ids)
        .filter(is_active=True, next_due_on__lte=today + datetime.timedelta(days=7))
        .count(),
        no_service_time=reliability.asset_count > 0 and reliability.calendar_hours == 0,
    )
