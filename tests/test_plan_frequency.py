"""Frecuencia por calendario en días, semanas o meses (PM-02 CA1), con fechas conocidas."""

import datetime as dt
from decimal import Decimal

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.core.tenancy import tenant_context
from apps.maintenance.models import CalendarUnit, MaintenancePlan, add_months, format_number
from apps.maintenance.services import (
    assign_plan,
    create_plan,
    publish_version,
    register_completion,
    start_draft,
)
from tests.conftest import login

ITEM = {"text": "Revisar", "item_type": "check"}
SIX_MONTHS = {"calendar_interval": 6, "calendar_unit": "months"}


@pytest.mark.parametrize(
    "start,months,expected",
    [
        (dt.date(2026, 1, 31), 1, dt.date(2026, 2, 28)),  # fin de mes ajustado
        (dt.date(2028, 1, 31), 1, dt.date(2028, 2, 29)),  # año bisiesto
        (dt.date(2028, 2, 29), 12, dt.date(2029, 2, 28)),  # 29-feb + 1 año
        (dt.date(2026, 12, 15), 1, dt.date(2027, 1, 15)),  # cambio de año
        (dt.date(2026, 3, 31), -1, dt.date(2026, 2, 28)),  # hacia atrás
        (dt.date(2026, 1, 31), 3, dt.date(2026, 4, 30)),
    ],
)
def test_add_months_uses_calendar_months_clamped_to_month_end(start, months, expected):
    assert add_months(start, months) == expected


def _plan(code, interval, unit, **kwargs):
    return create_plan(
        code=code, name=code, calendar_interval=interval, calendar_unit=unit, items=[ITEM], **kwargs
    )


def test_monthly_floating_counts_from_the_real_execution(in_a):
    plan = _plan("M1", 1, CalendarUnit.MONTHS)
    assignment = assign_plan(plan=plan, asset=in_a.asset, starts_on=dt.date(2028, 1, 10))
    register_completion(assignment=assignment, completed_on=dt.date(2028, 1, 31))
    assert assignment.next_due_on == dt.date(2028, 2, 29)
    register_completion(assignment=assignment, completed_on=dt.date(2028, 2, 29))
    assert assignment.next_due_on == dt.date(2028, 3, 29)


def test_monthly_fixed_keeps_the_scheduled_day(in_a):
    plan = _plan("M2", 1, CalendarUnit.MONTHS, schedule_mode="fixed")
    assignment = assign_plan(plan=plan, asset=in_a.asset, starts_on=dt.date(2026, 1, 31))
    register_completion(assignment=assignment, completed_on=dt.date(2026, 2, 3))
    assert assignment.next_due_on == dt.date(2026, 2, 28)
    # Muy atrasada: salta al primer objetivo posterior al cierre, sin quedar en el pasado.
    assignment = assign_plan(plan=plan, asset=in_a.asset, starts_on=dt.date(2026, 1, 31))
    register_completion(assignment=assignment, completed_on=dt.date(2026, 5, 2))
    assert assignment.next_due_on == dt.date(2026, 5, 31)


@pytest.mark.parametrize(
    "starts,closes,expected",
    [
        # 31-ene → 28/29-feb → 31-mar: el ajuste de febrero no arrastra el día.
        (
            dt.date(2026, 1, 31),
            [dt.date(2026, 1, 31), dt.date(2026, 2, 28)],
            [dt.date(2026, 2, 28), dt.date(2026, 3, 31)],
        ),
        (
            dt.date(2028, 1, 31),
            [dt.date(2028, 1, 31), dt.date(2028, 2, 29)],
            [dt.date(2028, 2, 29), dt.date(2028, 3, 31)],
        ),
        # 31-oct → 30-nov → 31-dic.
        (
            dt.date(2026, 10, 31),
            [dt.date(2026, 11, 2), dt.date(2026, 12, 1)],
            [dt.date(2026, 11, 30), dt.date(2026, 12, 31)],
        ),
        # 30 de cada mes: febrero lo ajusta y marzo vuelve al 30.
        (
            dt.date(2027, 1, 30),
            [dt.date(2027, 1, 30), dt.date(2027, 2, 28)],
            [dt.date(2027, 2, 28), dt.date(2027, 3, 30)],
        ),
    ],
)
def test_monthly_fixed_returns_to_the_anchor_day(in_a, starts, closes, expected):
    plan = _plan("MA", 1, CalendarUnit.MONTHS, schedule_mode="fixed")
    assignment = assign_plan(plan=plan, asset=in_a.asset, starts_on=starts)
    assert assignment.anchor_day == starts.day
    dates = []
    for closed_on in closes:
        register_completion(assignment=assignment, completed_on=closed_on)
        dates.append(assignment.next_due_on)
    assert dates == expected


def test_fixed_leap_day_anchor_over_years(in_a):
    plan = _plan("A29", 12, CalendarUnit.MONTHS, schedule_mode="fixed")
    assignment = assign_plan(plan=plan, asset=in_a.asset, starts_on=dt.date(2028, 2, 29))
    register_completion(assignment=assignment, completed_on=dt.date(2028, 2, 29))
    assert assignment.next_due_on == dt.date(2029, 2, 28)
    for year in (2030, 2031, 2032):
        register_completion(assignment=assignment, completed_on=assignment.next_due_on)
        assert assignment.next_due_on == dt.date(year, 2, 29 if year == 2032 else 28)


def test_quarterly_fixed_mode(in_a):
    plan = _plan("Q1", 3, CalendarUnit.MONTHS, schedule_mode="fixed")
    assignment = assign_plan(plan=plan, asset=in_a.asset, starts_on=dt.date(2026, 11, 30))
    register_completion(assignment=assignment, completed_on=dt.date(2026, 11, 28))
    assert assignment.next_due_on == dt.date(2027, 2, 28)


def test_weeks_are_seven_days(in_a, today):
    plan = _plan("W2", 2, CalendarUnit.WEEKS)
    assignment = assign_plan(plan=plan, asset=in_a.asset)
    assert assignment.next_due_on - timezone.localdate() == dt.timedelta(days=14)
    register_completion(assignment=assignment, completed_on=today)
    assert assignment.next_due_on == dt.date(2026, 10, 8)
    fixed = _plan("W3", 1, CalendarUnit.WEEKS, schedule_mode="fixed")
    assignment = assign_plan(plan=fixed, asset=in_a.asset, starts_on=today)
    register_completion(assignment=assignment, completed_on=today + dt.timedelta(days=20))
    assert assignment.next_due_on == dt.date(2026, 10, 15)  # 24-sep + 3 semanas


def test_changing_unit_on_publish_reschedules_from_last_execution(in_a):
    assignment = assign_plan(plan=in_a.plan, asset=in_a.asset, starts_on=dt.date(2026, 3, 2))
    assignment.last_done_on = dt.date(2026, 1, 31)
    assignment.save()
    draft = start_draft(plan=in_a.plan)
    draft.calendar_interval, draft.calendar_unit = 1, CalendarUnit.MONTHS
    draft.save()
    publish_version(version=draft)
    assignment.refresh_from_db()
    assert assignment.next_due_on == dt.date(2026, 2, 28)


def test_never_done_assignment_uses_start_of_previous_monthly_cycle(in_a):
    plan = _plan("M3", 1, CalendarUnit.MONTHS)
    assignment = assign_plan(plan=plan, asset=in_a.asset, starts_on=dt.date(2026, 3, 31))
    draft = start_draft(plan=plan)
    draft.calendar_interval, draft.calendar_unit = 2, CalendarUnit.WEEKS
    draft.save()
    publish_version(version=draft)
    assignment.refresh_from_db()
    assert assignment.next_due_on == dt.date(2026, 3, 14)  # 28-feb (ciclo anterior) + 14 días


@pytest.mark.parametrize(
    "fields,label",
    [
        ({"calendar_interval": 1, "calendar_unit": "months"}, "1 mes"),
        ({"calendar_interval": 6, "calendar_unit": "months"}, "6 meses"),
        ({"calendar_interval": 1, "calendar_unit": "weeks"}, "1 semana"),
        ({"calendar_interval": 30, "calendar_unit": "days"}, "30 días"),
        ({"meter_kind": "km", "meter_interval": Decimal(10000), **SIX_MONTHS}, "10.000 km o 6 meses"),
        ({"meter_kind": "hours", "meter_interval": Decimal("1250.5")}, "1.250,5 h"),
    ],
)
def test_frequency_label(in_a, fields, label):
    plan = create_plan(code="L", name="L", items=[ITEM], **fields)
    assert plan.frequency_label == label


@pytest.mark.parametrize(
    "value,text",
    [
        (Decimal("10000"), "10.000"),
        (Decimal("250.000"), "250"),
        (Decimal("1234567.25"), "1.234.567,25"),
        (5, "5"),
    ],
)
def test_format_number_uses_es_cl_separators(value, text):
    assert format_number(value) == text


def test_draft_screen_saves_the_unit(client, tenant_a):
    from apps.accounts.models import Membership

    login(client, Membership.objects.get(pk=tenant_a.admin.pk))
    with tenant_context(tenant_a.org):
        draft = start_draft(plan=tenant_a.plan)
    data = {
        "action": "header",
        "plan-code": "PM1",
        "plan-name": "Mensual",
        "plan-is_active": "on",
        "trigger-calendar_interval": "3",
        "trigger-calendar_unit": "months",
        "trigger-schedule_mode": "fixed",
        "trigger-tolerance_pct": "10",
        "trigger-estimated_minutes": "60",
    }
    response = client.post(reverse("plan_draft", args=[tenant_a.plan.pk]), data)
    assert response.status_code == 302
    with tenant_context(tenant_a.org):
        draft.refresh_from_db()
        assert (draft.calendar_interval, draft.calendar_unit) == (3, "months")
        assert MaintenancePlan.objects.get(pk=tenant_a.plan.pk).frequency_label == "30 días"
