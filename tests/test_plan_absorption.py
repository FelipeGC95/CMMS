"""Absorción de planes (PM-05 CA3) con fechas y lecturas conocidas.

Regla: si un plan mayor vence en un activo y un plan que absorbe vence en el
mismo activo a más tardar en el vencimiento del mayor + su tolerancia
(`tolerance_pct` de la frecuencia del mayor), se genera una sola OT con los dos
checklists en secciones rotuladas; al cerrarla avanzan ambas asignaciones.
"""

import datetime as dt
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.utils import timezone

from apps.assets.models import Meter, MeterKind
from apps.maintenance.models import CalendarUnit, PlanAbsorption
from apps.maintenance.services import (
    add_plan_absorption,
    assign_plan,
    create_plan,
    generate_due_work_orders,
    remove_plan_absorption,
)
from apps.work.models import WorkOrder, WorkOrderAbsorption
from apps.work.services import _close

TODAY = dt.date(2026, 9, 24)


def _plans(in_a, *, absorb=True):
    semestral = create_plan(
        code="SEM", name="Semestral", calendar_interval=6, calendar_unit=CalendarUnit.MONTHS,
        tolerance_pct=10, items=[{"text": "Cambiar aceite", "item_type": "check"}],
    )  # fmt: skip
    monthly = create_plan(
        code="MEN", name="Mensual", calendar_interval=1, calendar_unit=CalendarUnit.MONTHS,
        items=[{"text": "Limpiar filtro", "item_type": "check"}, {"text": "Engrasar", "item_type": "check"}],
    )  # fmt: skip
    if absorb:
        add_plan_absorption(membership=in_a.admin, plan=semestral, absorbed_plan=monthly)
    return semestral, monthly


def _hours_plan(code, hours, **kwargs):
    item = {"text": f"Tarea {code}", "item_type": "check"}
    return create_plan(
        code=code, name=f"{hours} h", meter_kind="hours", meter_interval=hours, items=[item], **kwargs
    )


def _close_on(wo, day, meter_value=None):
    if meter_value is not None:
        wo.meter_value_at_completion = meter_value
        wo.save(update_fields=["meter_value_at_completion"])
    at = timezone.make_aware(dt.datetime.combine(day, dt.time(12)))
    _close(work_order=wo, user=None, at=at)


def test_minor_due_within_tolerance_is_absorbed_into_one_work_order(in_a):
    semestral, monthly = _plans(in_a)
    big = assign_plan(plan=semestral, asset=in_a.asset, starts_on=dt.date(2026, 10, 1))
    small = assign_plan(plan=monthly, asset=in_a.asset, starts_on=dt.date(2026, 10, 5))

    created = generate_due_work_orders(today=TODAY, horizon_days=14)

    assert len(created) == 1
    wo = created[0]
    assert (wo.plan_assignment_id, wo.due_on, wo.title) == (big.pk, dt.date(2026, 10, 1), "Semestral")
    assert [(i.order, i.section, i.text) for i in wo.checklist.all()] == [
        (1, "Semestral", "Cambiar aceite"),
        (2, "Mensual", "Limpiar filtro"),
        (3, "Mensual", "Engrasar"),
    ]
    absorption = WorkOrderAbsorption.objects.get(work_order=wo)
    assert (absorption.plan_assignment_id, absorption.plan_version_id) == (
        small.pk,
        monthly.current_version_id,
    )
    # Idempotente: ni el mayor ni el menor generan otra OT mientras esta siga abierta.
    assert generate_due_work_orders(today=dt.date(2026, 10, 6), horizon_days=14) == []


def test_tolerance_window_is_ten_percent_of_the_major_cycle(in_a):
    """Ciclo 1-abr → 1-oct = 183 días; 10 % = 18 días: absorbe hasta el 19-oct."""
    semestral, monthly = _plans(in_a)
    assign_plan(plan=semestral, asset=in_a.asset, starts_on=dt.date(2026, 10, 1))
    small = assign_plan(plan=monthly, asset=in_a.asset, starts_on=dt.date(2026, 10, 20))
    [wo] = generate_due_work_orders(today=TODAY, horizon_days=14)
    assert not wo.absorptions.exists() and wo.checklist.count() == 1
    small.next_due_on = dt.date(2026, 10, 19)
    small.save()
    wo.status = WorkOrder.Status.CANCELLED
    wo.save()
    [wo] = generate_due_work_orders(today=TODAY, horizon_days=14)
    assert wo.absorptions.get().plan_assignment_id == small.pk


def test_without_absorption_each_plan_gets_its_own_work_order(in_a):
    _plans_ = _plans(in_a, absorb=False)
    for plan, day in zip(_plans_, (dt.date(2026, 10, 1), dt.date(2026, 10, 5)), strict=True):
        assign_plan(plan=plan, asset=in_a.asset, starts_on=day)
    assert len(generate_due_work_orders(today=TODAY, horizon_days=14)) == 2


def test_absorption_only_applies_on_the_same_asset(in_a):
    from apps.assets.models import Asset

    semestral, monthly = _plans(in_a)
    other = Asset.objects.create(code="CMP-02", name="Compresor 2", asset_class=in_a.asset.asset_class)
    assign_plan(plan=semestral, asset=in_a.asset, starts_on=dt.date(2026, 10, 1))
    assign_plan(plan=monthly, asset=other, starts_on=dt.date(2026, 10, 1))
    created = generate_due_work_orders(today=TODAY, horizon_days=14)
    assert len(created) == 2 and not WorkOrderAbsorption.objects.exists()


def test_closing_the_work_order_advances_both_assignments(in_a):
    semestral, monthly = _plans(in_a)
    big = assign_plan(plan=semestral, asset=in_a.asset, starts_on=dt.date(2026, 10, 1))
    small = assign_plan(plan=monthly, asset=in_a.asset, starts_on=dt.date(2026, 10, 5))
    [wo] = generate_due_work_orders(today=TODAY, horizon_days=14)

    _close_on(wo, dt.date(2026, 10, 2))

    big.refresh_from_db()
    small.refresh_from_db()
    assert (big.last_done_on, big.next_due_on) == (dt.date(2026, 10, 2), dt.date(2027, 4, 2))
    assert (small.last_done_on, small.next_due_on) == (dt.date(2026, 10, 2), dt.date(2026, 11, 2))
    # Ya cerrada: el menor vuelve a generar su propia OT cuando vence.
    [own] = generate_due_work_orders(today=dt.date(2026, 10, 25), horizon_days=14)
    assert own.plan_assignment_id == small.pk


def test_meter_plans_500_h_absorbs_250_h(in_a):
    meter = Meter.objects.create(asset=in_a.asset, kind=MeterKind.HOURS, current_value=Decimal(990))
    big_plan = _hours_plan("H500", 500, tolerance_pct=10)
    small_plan = _hours_plan("H250", 250)
    add_plan_absorption(membership=in_a.admin, plan=big_plan, absorbed_plan=small_plan)
    big = assign_plan(plan=big_plan, asset=in_a.asset, meter=meter, next_due_meter_value=Decimal(1000))
    small = assign_plan(plan=small_plan, asset=in_a.asset, meter=meter, next_due_meter_value=Decimal(1000))

    [wo] = generate_due_work_orders(today=TODAY, horizon_days=14)
    assert wo.plan_assignment_id == big.pk and wo.absorptions.get().plan_assignment_id == small.pk
    _close_on(wo, TODAY, meter_value=Decimal(1004))

    big.refresh_from_db()
    small.refresh_from_db()
    assert (big.next_due_meter_value, small.next_due_meter_value) == (Decimal(1504), Decimal(1254))


def test_meter_minor_outside_tolerance_is_not_absorbed(in_a):
    meter = Meter.objects.create(asset=in_a.asset, kind=MeterKind.HOURS, current_value=Decimal(990))
    big_plan = _hours_plan("H500", 500)
    small_plan = _hours_plan("H250", 250)
    add_plan_absorption(membership=in_a.admin, plan=big_plan, absorbed_plan=small_plan)
    assign_plan(plan=big_plan, asset=in_a.asset, meter=meter, next_due_meter_value=Decimal(1000))
    assign_plan(
        plan=small_plan, asset=in_a.asset, meter=meter, next_due_meter_value=Decimal(1051)
    )  # > 1000 + 50
    [wo] = generate_due_work_orders(today=TODAY, horizon_days=14)
    assert not wo.absorptions.exists()


def test_absorption_rules_reject_self_and_cycles(in_a):
    semestral, monthly = _plans(in_a)
    with pytest.raises(ValidationError):
        add_plan_absorption(membership=in_a.admin, plan=monthly, absorbed_plan=semestral)
    with pytest.raises(ValidationError):
        add_plan_absorption(membership=in_a.admin, plan=semestral, absorbed_plan=semestral)
    again = add_plan_absorption(membership=in_a.admin, plan=semestral, absorbed_plan=monthly)
    assert PlanAbsorption.objects.count() == 1
    remove_plan_absorption(membership=in_a.admin, absorption=again)
    assert not PlanAbsorption.objects.exists()
