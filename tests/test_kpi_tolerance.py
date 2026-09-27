"""Cumplimiento preventivo (KPI-01) con la tolerancia del plan (PM-02 CA3): resultados conocidos.

Plan mensual con 10 % de tolerancia: el ciclo que vence el 1-feb dura 31 días → ±3 días.
Plan por horómetro cada 500 h con 10 % → ±50 h.
"""

import datetime as dt
from decimal import Decimal

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.maintenance.models import CalendarUnit
from apps.maintenance.selectors import is_on_time
from apps.maintenance.services import create_plan
from apps.reporting.selectors import KpiFilters, _compliance_outcome, dashboard_kpis
from apps.work.models import WorkOrder
from apps.work.services import create_work_order

ITEM = {"text": "Revisar", "item_type": "check"}
NOW = timezone.make_aware(dt.datetime(2026, 2, 11, 12))
DUE = dt.date(2026, 2, 1)


def _aware(day):
    return timezone.make_aware(dt.datetime.combine(day, dt.time(12)))


@pytest.fixture
def monthly(in_a):
    return create_plan(
        code="M",
        name="M",
        calendar_interval=1,
        calendar_unit=CalendarUnit.MONTHS,
        tolerance_pct=10,
        items=[ITEM],
    )


@pytest.fixture
def hourly(in_a):
    return create_plan(
        code="H", name="H", meter_kind="hours", meter_interval=500, tolerance_pct=10, items=[ITEM]
    )


def _pm(in_a, plan=None, *, due_on=DUE, done_on=None, due_meter=None, reading=None):
    wo = create_work_order(
        work_type="preventive",
        title="PM",
        asset=in_a.asset,
        plan_version=plan.current_version if plan else None,
        due_on=due_on,
        due_meter_value=due_meter,
    )
    if done_on is not None:
        WorkOrder.objects.filter(pk=wo.pk).update(
            status="completed", completed_at=_aware(done_on), meter_value_at_completion=reading
        )
    return wo


def _compliance(in_a):
    return dashboard_kpis(
        membership=in_a.admin, now=NOW, filters=KpiFilters(days=30)
    ).preventive_compliance_pct


@pytest.mark.parametrize(
    "done_on,expected",
    [
        (dt.date(2026, 2, 3), 100),  # tarde, pero dentro de la tolerancia
        (dt.date(2026, 2, 5), 0),  # más allá de la tolerancia
        (dt.date(2026, 1, 29), 100),  # temprano, dentro de la tolerancia
        (dt.date(2026, 1, 28), 0),  # demasiado temprano
    ],
)
def test_calendar_tolerance(in_a, monthly, done_on, expected):
    _pm(in_a, monthly, done_on=done_on)
    assert _compliance(in_a) == expected


def test_meter_tolerance_is_enough(in_a, hourly):
    """Sin frecuencia por calendario la ventana en días es 0, pero basta cumplir por medidor."""
    _pm(in_a, hourly, done_on=dt.date(2026, 2, 5), due_meter=Decimal(1000), reading=Decimal(1040))
    _pm(in_a, hourly, done_on=dt.date(2026, 2, 5), due_meter=Decimal(1000), reading=Decimal(1060))
    assert _compliance(in_a) == 50


def test_open_within_tolerance_is_not_counted_yet(in_a, monthly):
    """Vence el 9-feb (±3 días): el 11-feb aún puede cumplirse → fuera del denominador."""
    _pm(in_a, monthly, due_on=dt.date(2026, 2, 9))
    assert _compliance(in_a) is None
    _pm(in_a, monthly, due_on=dt.date(2026, 2, 5))  # su ventana terminó el 8-feb → incumplida
    _pm(in_a, monthly, done_on=DUE)
    assert _compliance(in_a) == 50


def test_manual_preventive_without_plan_uses_zero_tolerance(in_a):
    _pm(in_a, done_on=DUE)
    _pm(in_a, done_on=dt.date(2026, 2, 2))
    _pm(in_a, due_on=dt.date(2026, 2, 3))  # abierta y vencida
    assert round(_compliance(in_a), 6) == round(100 / 3, 6)


def test_cancelled_do_not_count(in_a, monthly):
    wo = _pm(in_a, monthly)
    WorkOrder.objects.filter(pk=wo.pk).update(status="cancelled")
    assert _compliance(in_a) is None


@pytest.mark.parametrize("done_on", [dt.date(2026, 1, 28), dt.date(2026, 1, 29), dt.date(2026, 2, 4), None])
def test_outcome_agrees_with_is_on_time(in_a, monthly, done_on):
    wo = WorkOrder.objects.select_related("plan_version").get(pk=_pm(in_a, monthly, done_on=done_on).pk)
    outcome = _compliance_outcome(wo, NOW)
    if done_on is not None:
        assert outcome is is_on_time(wo)
    else:
        assert outcome is False  # vencía el 1-feb: la ventana terminó el 4-feb


def test_compliance_queries_do_not_grow_with_work_orders(in_a, monthly, hourly):
    _pm(in_a, monthly, done_on=DUE)

    def count():
        with CaptureQueriesContext(connection) as ctx:
            _compliance(in_a)
        return len(ctx)

    few = count()
    for day in range(1, 8):
        _pm(in_a, monthly, done_on=dt.date(2026, 2, day))
        _pm(in_a, hourly, done_on=DUE, due_meter=Decimal(500), reading=Decimal(500 + day))
    assert count() == few
