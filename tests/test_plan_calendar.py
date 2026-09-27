"""Proyección del calendario de preventivos (PM-06 CA1) con fechas y lecturas conocidas."""

import datetime as dt
from decimal import Decimal

import pytest

from apps.assets.models import Meter, MeterKind
from apps.maintenance.models import CalendarUnit, PlanVersion
from apps.maintenance.selectors import calendar_months, plan_calendar
from apps.maintenance.services import add_plan_absorption, assign_plan, create_plan, generate_due_work_orders

TODAY = dt.date(2026, 9, 24)
END = dt.date(2027, 9, 30)
ITEM = [{"text": "Revisar", "item_type": "check"}]


def _calendar(tenant, **kwargs):
    return plan_calendar(membership=tenant.admin, start=TODAY, end=END, today=TODAY, **kwargs)


def _dates(calendar, plan=None):
    return [o.date for o in calendar.occurrences if plan is None or o.plan == plan]


def _plan(code, **fields):
    return create_plan(code=code, name=code, items=ITEM, **fields)


def _meter(tenant, value=990, avg=8):
    return Meter.objects.create(
        asset=tenant.asset,
        kind=MeterKind.HOURS,
        current_value=Decimal(value),
        avg_daily_usage=Decimal(avg) if avg is not None else None,
    )


@pytest.mark.parametrize(
    "interval,unit,starts,expected",
    [
        (10, CalendarUnit.DAYS, dt.date(2026, 10, 1), [dt.date(2026, 10, 1), dt.date(2026, 10, 11)]),
        (2, CalendarUnit.WEEKS, dt.date(2026, 10, 1), [dt.date(2026, 10, 1), dt.date(2026, 10, 15)]),
        # Fin de mes: 31-ene + 1 mes = 28-feb, y desde ahí se sigue contando.
        (1, CalendarUnit.MONTHS, dt.date(2027, 1, 31), [dt.date(2027, 1, 31), dt.date(2027, 2, 28)]),
    ],
)
def test_calendar_steps_by_unit(in_a, interval, unit, starts, expected):
    plan = _plan("CAL", calendar_interval=interval, calendar_unit=unit)
    assign_plan(plan=plan, asset=in_a.asset, starts_on=starts)
    dates = _dates(_calendar(in_a))
    assert dates[:2] == expected
    assert all(TODAY <= d <= END for d in dates)
    assert all(o.trigger == "calendar" and not o.is_work_order for o in _calendar(in_a).occurrences)


def test_floating_month_end_counts_from_each_projected_execution(in_a):
    """Flotante: cada ciclo parte de la ejecución supuesta (el vencimiento ajustado)."""
    plan = _plan("FIN", calendar_interval=1, calendar_unit=CalendarUnit.MONTHS)
    assign_plan(plan=plan, asset=in_a.asset, starts_on=dt.date(2026, 10, 31))
    assert _dates(_calendar(in_a))[:3] == [
        dt.date(2026, 10, 31),
        dt.date(2026, 11, 30),
        dt.date(2026, 12, 30),
    ]


def test_fixed_month_end_returns_to_the_anchor_day(in_a):
    """Fijo: vuelve al día 31 tras cada mes corto, también en febrero bisiesto."""
    plan = _plan("FIJ", calendar_interval=1, calendar_unit=CalendarUnit.MONTHS, schedule_mode="fixed")
    assign_plan(plan=plan, asset=in_a.asset, starts_on=dt.date(2026, 10, 31))
    assert _dates(_calendar(in_a))[:6] == [
        dt.date(2026, 10, 31),
        dt.date(2026, 11, 30),
        dt.date(2026, 12, 31),
        dt.date(2027, 1, 31),
        dt.date(2027, 2, 28),
        dt.date(2027, 3, 31),
    ]


def test_meter_projection_uses_average_daily_usage(in_a):
    meter = _meter(in_a)  # 990 h, 8 h/día
    plan = _plan("H250", meter_kind="hours", meter_interval=250)
    assign_plan(plan=plan, asset=in_a.asset, meter=meter, next_due_meter_value=Decimal(1000))
    occurrences = _calendar(in_a).occurrences
    # 10 h faltantes / 8 = 2 días; luego 1006 + 250 → 34 días; 1262 + 250 → 66 días.
    assert [o.date for o in occurrences[:3]] == [
        dt.date(2026, 9, 26),
        dt.date(2026, 10, 28),
        dt.date(2026, 11, 29),
    ]
    assert {o.trigger for o in occurrences} == {"meter"}


def test_fixed_meter_projection_keeps_the_grid(in_a):
    meter = _meter(in_a)
    plan = _plan(
        "H250F", meter_kind="hours", meter_interval=250, schedule_mode=PlanVersion.ScheduleMode.FIXED
    )
    assign_plan(plan=plan, asset=in_a.asset, meter=meter, next_due_meter_value=Decimal(1000))
    # Objetivo siguiente 1250 (no 1256): 260 h / 8 = 33 días.
    assert _dates(_calendar(in_a))[:2] == [dt.date(2026, 9, 26), dt.date(2026, 10, 27)]


def test_meter_without_average_usage_is_not_projected(in_a):
    meter = _meter(in_a, avg=None)
    plan = _plan("H500", meter_kind="hours", meter_interval=500)
    assignment = assign_plan(plan=plan, asset=in_a.asset, meter=meter)
    calendar = _calendar(in_a)
    assert calendar.occurrences == []
    assert calendar.unestimated == [assignment]


def test_mixed_without_average_usage_projects_by_calendar_and_flags_it(in_a):
    meter = _meter(in_a, avg=None)
    plan = _plan("MIX0", meter_kind="hours", meter_interval=500, calendar_interval=3, calendar_unit="months")
    assign_plan(plan=plan, asset=in_a.asset, meter=meter, starts_on=dt.date(2026, 10, 1))
    calendar = _calendar(in_a)
    assert _dates(calendar)[:2] == [dt.date(2026, 10, 1), dt.date(2027, 1, 1)]
    assert all(o.no_usage for o in calendar.occurrences)
    assert calendar.unestimated == []


@pytest.mark.parametrize(
    "mode,second",
    [(PlanVersion.ScheduleMode.FLOATING, dt.date(2026, 10, 26)), ("fixed", dt.date(2026, 10, 27))],
)
def test_mixed_whichever_first_floating_vs_fixed(in_a, mode, second):
    meter = _meter(in_a)
    plan = _plan(
        "MIX", meter_kind="hours", meter_interval=250, calendar_interval=1, calendar_unit="months",
        schedule_mode=mode,
    )  # fmt: skip
    assign_plan(
        plan=plan,
        asset=in_a.asset,
        meter=meter,
        starts_on=dt.date(2026, 10, 24),
        next_due_meter_value=Decimal(1000),
    )
    occurrences = _calendar(in_a).occurrences
    # Primero vence por uso (26-sep). Flotante: el calendario se reinicia desde ahí (26-oct);
    # fijo: el calendario pasa a 24-nov y gana el medidor (1250 h → 27-oct).
    assert (occurrences[0].date, occurrences[0].trigger) == (dt.date(2026, 9, 26), "meter")
    assert occurrences[1].date == second


@pytest.mark.parametrize(
    "mode,second", [("floating", dt.date(2026, 10, 24)), ("fixed", dt.date(2026, 10, 1))]
)
def test_overdue_is_shown_today_and_reschedules_by_mode(in_a, mode, second):
    plan = _plan("VEN", calendar_interval=1, calendar_unit="months", schedule_mode=mode)
    assign_plan(plan=plan, asset=in_a.asset, starts_on=dt.date(2026, 9, 1))
    occurrences = _calendar(in_a).occurrences
    assert (occurrences[0].date, occurrences[0].overdue) == (TODAY, True)
    assert (occurrences[1].date, occurrences[1].overdue) == (second, False)


def _absorbing_plans(in_a):
    semestral = _plan("SEM", calendar_interval=6, calendar_unit="months", estimated_minutes=240)
    monthly = _plan("MEN", calendar_interval=1, calendar_unit="months", estimated_minutes=30)
    add_plan_absorption(membership=in_a.admin, plan=semestral, absorbed_plan=monthly)
    assign_plan(plan=semestral, asset=in_a.asset, starts_on=dt.date(2026, 10, 1))
    assign_plan(plan=monthly, asset=in_a.asset, starts_on=dt.date(2026, 10, 5))
    return semestral, monthly


def test_absorbed_occurrences_join_the_major_one(in_a):
    semestral, monthly = _absorbing_plans(in_a)
    calendar = _calendar(in_a)
    first_major = next(o for o in calendar.occurrences if o.plan == semestral)
    [minor] = first_major.absorbed
    assert (minor.plan, minor.date, minor.absorbed_into) == (monthly, dt.date(2026, 10, 1), first_major)
    assert first_major.estimated_minutes == 270 and minor.estimated_minutes == 0
    # El menor sigue desde la ejecución conjunta; en abril vuelve a coincidir con el mayor.
    assert _dates(calendar, monthly)[:3] == [dt.date(2026, 10, 1), dt.date(2026, 11, 1), dt.date(2026, 12, 1)]
    april = [o for o in calendar.occurrences if o.date == dt.date(2027, 4, 1)]
    assert [(o.plan, o.absorbed_into is not None) for o in april] == [(semestral, False), (monthly, True)]
    # Filtrar por el plan menor conserva la absorción.
    only_minor = _calendar(in_a, plan=monthly)
    assert {o.plan for o in only_minor.occurrences} == {monthly}
    assert only_minor.occurrences[0].absorbed_into.plan == semestral


def test_generation_and_projection_agree_on_the_next_occurrence(in_a):
    semestral, monthly = _absorbing_plans(in_a)
    meter = _meter(in_a)
    hours = _plan("H250", meter_kind="hours", meter_interval=250)
    assign_plan(plan=hours, asset=in_a.asset, meter=meter, next_due_meter_value=Decimal(1000))
    projected = {}
    for o in _calendar(in_a).occurrences:
        projected.setdefault(o.plan, o)

    created = generate_due_work_orders(today=TODAY, horizon_days=14)

    generated = {wo.plan_assignment.plan: wo for wo in created}
    assert set(generated) == {semestral, hours}
    for plan, wo in generated.items():
        assert wo.due_on == projected[plan].date
    assert [a.plan_assignment.plan for a in generated[semestral].absorptions.all()] == [monthly]
    assert projected[monthly].absorbed_into is projected[semestral]


def test_open_work_order_replaces_the_next_projection(in_a):
    plan = _plan("CAL", calendar_interval=1, calendar_unit="months")
    assign_plan(plan=plan, asset=in_a.asset, starts_on=dt.date(2026, 10, 1))
    [wo] = generate_due_work_orders(today=TODAY, horizon_days=14)
    occurrences = _calendar(in_a).occurrences
    assert (occurrences[0].date, occurrences[0].work_order) == (dt.date(2026, 10, 1), wo)
    assert occurrences[1].date == dt.date(2026, 11, 1) and not occurrences[1].is_work_order
    assert sum(o.date == dt.date(2026, 10, 1) for o in occurrences) == 1


def test_twelve_month_summary_counts_and_minutes(in_a):
    _absorbing_plans(in_a)
    months = calendar_months(_calendar(in_a))
    assert len(months) == 13  # septiembre 2026 a septiembre 2027
    october = months[1]
    assert (october["month"], october["count"], october["absorbed"], october["minutes"]) == (
        dt.date(2026, 10, 1),
        1,
        1,
        270,
    )
    november = months[2]
    assert (november["count"], november["absorbed"], november["minutes"]) == (1, 0, 30)
