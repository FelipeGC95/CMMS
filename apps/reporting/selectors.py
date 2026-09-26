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


def _reliability(*, asset_count, periods, failures, window_start, now) -> Reliability:
    """Disponibilidad, MTTR y MTBF (KPI-01) sobre `asset_count` activos en la ventana.

    `periods`: períodos de detención (AssetStatusPeriod); `failures`: fallas
    con detención total iniciadas en la ventana.
    """
    window_seconds = (now - window_start).total_seconds()
    downtime = sum(
        _overlap_seconds(p.started_at, p.ended_at or now, window_start, now)
        for p in periods.filter(status__in=DOWN_STATUSES, started_at__lt=now)
        .exclude(ended_at__lt=window_start)
        .only("started_at", "ended_at")
    )
    availability = 100 * (1 - downtime / (asset_count * window_seconds)) if asset_count else None
    failures = list(
        failures.filter(impact=FailureEvent.Impact.TOTAL_STOP, started_at__gte=window_start).only(
            "started_at", "ended_at"
        )
    )
    repaired = [f for f in failures if f.ended_at]
    mttr = (
        sum((f.ended_at - f.started_at).total_seconds() for f in repaired) / len(repaired) / 3600
        if repaired
        else None
    )
    mtbf = (asset_count * window_seconds - downtime) / len(failures) / 3600 if failures else None
    return Reliability(availability, mttr, mtbf, len(failures))


def asset_kpis(*, membership, asset, include_components: bool = False, days: int = 30, now=None):
    """Disponibilidad, MTBF y MTTR de un activo y, opcionalmente, de sus componentes (AST-03 CA2).

    Se calculan sobre el conjunto (activo + componentes visibles) como si
    fuera la flota del tablero: la detención de cada uno descuenta de su propio
    tiempo calendario. Una falla cuenta si el activo o el componente afectado
    está en el conjunto. Los activos dados de baja no suman tiempo calendario.
    """
    now = now or timezone.now()
    window_start = now - datetime.timedelta(days=days)
    ids = asset_scope_ids(membership=membership, asset=asset, include_components=include_components)
    asset_count = (
        visible_assets(membership=membership)
        .filter(pk__in=ids, is_active=True)
        .exclude(status=Asset.Status.RETIRED)
        .count()
    )
    return _reliability(
        asset_count=asset_count,
        periods=AssetStatusPeriod.objects.filter(asset_id__in=ids),
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

    assets = visible_assets(membership=membership).filter(is_active=True).exclude(status=Asset.Status.RETIRED)
    reliability = _reliability(
        asset_count=assets.count(),
        periods=_by_asset_scope(AssetStatusPeriod.objects, location_ids),
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
    )
