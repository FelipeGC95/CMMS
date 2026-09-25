import datetime
import uuid
from decimal import Decimal

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.accounts.permissions import PermissionDeniedError
from apps.assets.models import Meter, MeterKind
from apps.assets.services import MeterReadingError, record_meter_reading
from apps.core.tenancy import tenant_context
from apps.maintenance.models import PlanVersion
from apps.maintenance.services import assign_plan, create_plan, generate_due_work_orders
from apps.work.models import WorkOrder
from apps.work.services import answer_checklist_item, complete_work_order
from tests.conftest import login


@pytest.fixture
def meter(in_a):
    return Meter.objects.create(asset=in_a.asset, kind=MeterKind.HOURS)


def _read(meter, value, days_ago=0, **kwargs):
    at = timezone.now() - datetime.timedelta(days=days_ago)
    return record_meter_reading(meter=meter, value=value, read_at=at, **kwargs)


def _usage_plan(in_a, interval=250, days=None, **fields):
    return create_plan(
        code=f"PM-H{interval}-{days}",
        name=f"Cada {interval} h",
        meter_kind=MeterKind.HOURS,
        meter_interval=Decimal(interval),
        interval_days=days,
        items=[{"text": "Cambiar aceite", "item_type": "check", "required": False}],
        **fields,
    )


# --- Lecturas ---------------------------------------------------------------


def test_reading_updates_current_value_and_daily_usage(meter):
    _read(meter, 1000, days_ago=10)
    _read(meter, 1080, days_ago=0)
    meter.refresh_from_db()
    assert meter.current_value == Decimal("1080")
    assert meter.unit == "h"
    assert meter.avg_daily_usage == Decimal("8.000")


def test_lower_reading_is_rejected_unless_reset(meter):
    _read(meter, 1000)
    with pytest.raises(MeterReadingError):
        _read(meter, 990)
    _read(meter, 5, is_reset=True)
    meter.refresh_from_db()
    assert meter.current_value == Decimal("5")


def test_historical_reading_does_not_change_current_value(meter):
    _read(meter, 1000, days_ago=1)
    _read(meter, 900, days_ago=5)
    meter.refresh_from_db()
    assert meter.current_value == Decimal("1000")
    assert meter.readings.count() == 2


def test_more_than_24_hours_per_day_warns_but_saves(meter):
    _read(meter, 1000, days_ago=1)
    _, warnings = _read(meter, 1100)
    assert warnings and "24 h" in warnings[0]
    meter.refresh_from_db()
    assert meter.current_value == Decimal("1100")


def test_reading_is_idempotent_by_client_uuid(meter):
    key = uuid.uuid4()
    first, _ = _read(meter, 1000, client_uuid=key)
    second, _ = _read(meter, 1000, client_uuid=key)
    assert first.pk == second.pk and meter.readings.count() == 1


def test_permissions_for_meter_readings(in_a, meter):
    record_meter_reading(meter=meter, value=10, membership=in_a.viewer)  # operador/chofer: sí
    external = in_a.viewer
    external.role = "external"
    with pytest.raises(PermissionDeniedError):
        record_meter_reading(meter=meter, value=20, membership=external)


# --- Preventivos por uso ----------------------------------------------------


def test_assign_usage_plan_requires_matching_meter(in_a):
    plan = _usage_plan(in_a)
    with pytest.raises(Exception, match="no tiene un medidor"):
        assign_plan(plan=plan, asset=in_a.asset)


def test_assign_usage_plan_targets_current_plus_interval(in_a, meter):
    _read(meter, 1000)
    assignment = assign_plan(plan=_usage_plan(in_a), asset=in_a.asset)
    assert assignment.meter == meter
    assert assignment.next_due_meter_value == Decimal("1250")
    assert assignment.next_due_on is None


def test_reading_that_reaches_threshold_generates_work_order(in_a, meter):
    _read(meter, 1000)
    assignment = assign_plan(plan=_usage_plan(in_a), asset=in_a.asset)
    _read(meter, 1100)
    assert not WorkOrder.objects.exists()
    _read(meter, 1230)  # dentro del 10 % previo al vencimiento (1250)
    wo = WorkOrder.objects.get()
    assert wo.plan_assignment == assignment and wo.due_meter_value == Decimal("1250")
    _read(meter, 1260)  # sigue abierta: no duplica
    assert WorkOrder.objects.count() == 1


def test_mixed_plan_is_due_by_whichever_comes_first(in_a, meter, today):
    _read(meter, 1000)
    plan = _usage_plan(in_a, interval=250, days=30)
    # Por fecha: vence hoy aunque el medidor esté lejos.
    assign_plan(plan=plan, asset=in_a.asset, starts_on=today)
    (wo,) = generate_due_work_orders(today=today)
    assert wo.due_on == today
    wo.status = WorkOrder.Status.CANCELLED
    wo.save()
    # Por uso: vence por medidor aunque la fecha esté lejos.
    assign_plan(
        plan=plan,
        asset=in_a.asset,
        starts_on=today + datetime.timedelta(days=60),
        next_due_meter_value=Decimal("1010"),
    )
    (wo,) = generate_due_work_orders(today=today)
    assert wo.due_meter_value == Decimal("1010")


def test_completion_reading_reschedules_usage_plan(in_a, meter):
    _read(meter, 1000)
    assignment = assign_plan(plan=_usage_plan(in_a), asset=in_a.asset, next_due_meter_value=Decimal("1020"))
    (wo,) = generate_due_work_orders()
    complete_work_order(work_order=wo, membership=in_a.tech, labor_minutes=60, meter_value=Decimal("1030"))
    assignment.refresh_from_db()
    meter.refresh_from_db()
    wo.refresh_from_db()
    assert wo.status == "closed" and wo.meter_value_at_completion == Decimal("1030")
    assert meter.current_value == Decimal("1030")
    assert assignment.last_done_meter_value == Decimal("1030")
    assert assignment.next_due_meter_value == Decimal("1280")


def test_meter_reset_keeps_remaining_usage(in_a, meter):
    _read(meter, 1000)
    assignment = assign_plan(plan=_usage_plan(in_a), asset=in_a.asset)  # vence a 1250
    _read(meter, 0, is_reset=True)
    assignment.refresh_from_db()
    assert assignment.next_due_meter_value == Decimal("250")


def test_plan_needs_some_frequency(in_a):
    from django.core.exceptions import ValidationError

    plan = PlanVersion(number=1)
    with pytest.raises(ValidationError):
        plan.clean()
    plan.meter_kind = MeterKind.KM
    with pytest.raises(ValidationError):
        plan.clean()


# --- Vistas -----------------------------------------------------------------


def test_meter_reading_view(client, tenant_a):
    with tenant_context(tenant_a.org):
        meter = Meter.objects.create(asset=tenant_a.asset, kind=MeterKind.KM)
    login(client, tenant_a.viewer)
    url = reverse("meter_reading", args=[tenant_a.asset.pk, meter.pk])
    response = client.post(url, {f"{meter.pk}-value": "12500"})
    assert response.status_code == 302
    with tenant_context(tenant_a.org):
        meter.refresh_from_db()
    assert meter.current_value == Decimal("12500")
    detail = client.get(reverse("asset_detail", args=[tenant_a.asset.pk]))
    assert "12.500" in detail.content.decode()


def test_meter_reading_view_is_isolated(client, tenant_a, tenant_b):
    with tenant_context(tenant_a.org):
        meter = Meter.objects.create(asset=tenant_a.asset, kind=MeterKind.KM)
    login(client, tenant_b.admin)
    url = reverse("meter_reading", args=[tenant_a.asset.pk, meter.pk])
    assert client.post(url, {f"{meter.pk}-value": "1"}).status_code == 404


def test_completion_form_requires_reading_for_usage_plans(client, in_a, meter):
    _read(meter, 1000)
    assign_plan(plan=_usage_plan(in_a), asset=in_a.asset, next_due_meter_value=Decimal("1010"))
    (wo,) = generate_due_work_orders()
    for item in wo.checklist.all():
        answer_checklist_item(item=item, membership=in_a.tech, result="ok")
    login(client, in_a.tech)
    url = reverse("work_order_complete", args=[wo.pk])
    assert client.post(url, {"labor_minutes": 30}).status_code == 400
    assert client.post(url, {"labor_minutes": 30, "meter_value": "1015"}).status_code == 302
    wo.refresh_from_db()
    assert wo.status == "closed"
