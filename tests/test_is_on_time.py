"""Cumplimiento dentro de la tolerancia (PM-02 CA3) con fechas y lecturas conocidas."""

import datetime as dt
from decimal import Decimal

import pytest
from django.utils import timezone

from apps.maintenance.models import CalendarUnit
from apps.maintenance.selectors import is_on_time
from apps.maintenance.services import create_plan
from apps.work.services import create_work_order

ITEM = {"text": "Revisar", "item_type": "check"}


def _wo(plan, *, due_on=None, due_meter=None, done_on=None, reading=None, work_type="preventive"):
    wo = create_work_order(
        work_type=work_type, title="x", asset=None or plan._asset, plan_version=plan.current_version,
        due_on=due_on, due_meter_value=due_meter,
    )  # fmt: skip
    if done_on:
        wo.completed_at = timezone.make_aware(dt.datetime.combine(done_on, dt.time(12)))
    wo.meter_value_at_completion = reading
    return wo


@pytest.fixture
def monthly(in_a):
    plan = create_plan(
        code="M",
        name="M",
        calendar_interval=1,
        calendar_unit=CalendarUnit.MONTHS,
        tolerance_pct=10,
        items=[ITEM],
    )
    plan._asset = in_a.asset
    return plan


@pytest.mark.parametrize(
    "done_on,expected",
    [
        # Ciclo 1-ene → 1-feb = 31 días; 10 % = 3 días.
        (dt.date(2026, 2, 1), True),
        (dt.date(2026, 2, 4), True),
        (dt.date(2026, 2, 5), False),
        (dt.date(2026, 1, 29), True),
        (dt.date(2026, 1, 28), False),
    ],
)
def test_calendar_window(monthly, done_on, expected):
    assert is_on_time(_wo(monthly, due_on=dt.date(2026, 2, 1), done_on=done_on)) is expected


def test_meter_window(in_a):
    plan = create_plan(
        code="H", name="H", meter_kind="hours", meter_interval=500, tolerance_pct=10, items=[ITEM]
    )
    plan._asset = in_a.asset
    on_time = _wo(plan, due_meter=Decimal(1000), done_on=dt.date(2026, 3, 1), reading=Decimal(1050))
    late = _wo(plan, due_meter=Decimal(1000), done_on=dt.date(2026, 3, 1), reading=Decimal("1050.5"))
    assert (is_on_time(on_time), is_on_time(late)) == (True, False)


def test_not_applicable_returns_none(monthly):
    assert is_on_time(_wo(monthly, due_on=dt.date(2026, 2, 1))) is None  # sin completar
    assert is_on_time(_wo(monthly, work_type="corrective", done_on=dt.date(2026, 2, 1))) is None
