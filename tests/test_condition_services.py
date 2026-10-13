"""Mantenimiento por condición (CBM-01..05): reglas, evaluación con histéresis, alertas y OT predictiva.

Base real (PostgreSQL en el CI), valores y fechas conocidos (`tests.condition_utils.BASE`).
"""

import uuid
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from apps.assets.models import Asset, Meter, MeterKind, MeterReading
from apps.assets.services import MeterReadingError, create_meter, record_meter_reading
from apps.audit.models import AuditEvent
from apps.condition import services
from apps.condition.models import ConditionAlert, ConditionRule, ConditionState
from apps.condition.services import ConditionError
from apps.maintenance.models import PlanVersion
from apps.maintenance.services import create_plan
from apps.notifications.models import Kind, Notification
from apps.organizations.models import Plan
from apps.organizations.modules import ModuleNotIncludedError
from apps.work.models import WorkOrder
from apps.work.services import transition
from tests.condition_utils import read, variable

Status = ConditionAlert.Status


@pytest.fixture
def temp(in_a):
    return variable(in_a.asset)


def _rule(in_a, meter=None, **kwargs):
    kwargs.setdefault("operator", "gt")
    if kwargs["operator"] in ("gt", "lt", "rate"):
        kwargs.setdefault("threshold", "80")
    if meter is not None:
        kwargs["meter"] = meter
    return services.create_rule(membership=in_a.admin, **kwargs)


def _state(rule, meter):
    return ConditionState.objects.get(rule=rule, meter=meter)


# --- Variables (CBM-01) -------------------------------------------------------------


def test_variable_kinds_are_never_cumulative_and_accept_negatives(in_a, temp):
    assert MeterKind.is_variable(MeterKind.TEMPERATURE)
    assert not MeterKind.is_variable(MeterKind.HOURS)
    assert temp.is_cumulative is False and temp.unit == "°C"
    read(temp, "-12.5")
    read(temp, "40", hours=1)
    read(temp, "35", hours=2)  # baja: una variable no es acumulativa
    temp.refresh_from_db()
    assert temp.current_value == Decimal("35")
    assert temp.avg_daily_usage is None  # sin uso diario ni proyección


def test_usage_meter_still_rejects_negatives(in_a):
    hours = Meter.objects.create(asset=in_a.asset, kind=MeterKind.HOURS)
    with pytest.raises(MeterReadingError):
        read(hours, "-1")


def test_create_meter_forces_non_cumulative_for_variables(in_a):
    meter = create_meter(membership=in_a.admin, asset=in_a.asset, kind=MeterKind.VIBRATION, name="Motor")
    assert meter.is_cumulative is False and meter.unit == "mm/s"
    usage = create_meter(membership=in_a.admin, asset=in_a.asset, kind=MeterKind.HOURS)
    assert usage.is_cumulative is True
    with pytest.raises(ValidationError):
        create_meter(membership=in_a.admin, asset=in_a.asset, kind=MeterKind.VIBRATION, name="Motor")
    assert AuditEvent.objects.filter(target_id=str(meter.pk), action="create").exists()


def test_plans_cannot_trigger_on_variable_kinds(in_a):
    with pytest.raises(ValidationError):
        create_plan(
            code="PMT",
            name="Por temperatura",
            meter_kind=MeterKind.TEMPERATURE,
            meter_interval=Decimal(10),
            items=[{"text": "x", "item_type": "check"}],
        )
    from apps.maintenance.forms import TriggerForm

    kinds = dict(TriggerForm.base_fields["meter_kind"].choices)
    assert MeterKind.HOURS in kinds and MeterKind.TEMPERATURE not in kinds
    assert PlanVersion._meta.get_field("meter_kind").max_length == 20


# --- Reglas (CBM-03) ---------------------------------------------------------------------


def test_rule_validations(in_a, temp):
    with pytest.raises(ConditionError):
        _rule(in_a, temp, operator="outside", min_value="80", max_value="40")
    with pytest.raises(ConditionError):
        _rule(in_a, temp, operator="rate", threshold="0")
    with pytest.raises(ConditionError):
        _rule(in_a, temp, operator="gt", threshold="")
    with pytest.raises(ConditionError):
        _rule(in_a, temp, consecutive=0)
    with pytest.raises(ConditionError):  # alcance doble
        _rule(in_a, temp, asset_class=in_a.asset.asset_class, meter_kind=MeterKind.TEMPERATURE)
    with pytest.raises(ConditionError):  # sin alcance
        _rule(in_a)
    hours = Meter.objects.create(asset=in_a.asset, kind=MeterKind.HOURS)
    with pytest.raises(ConditionError):  # medidor de uso
        _rule(in_a, hours)
    with pytest.raises(ConditionError):  # clase con tipo de uso
        _rule(in_a, asset_class=in_a.asset.asset_class, meter_kind=MeterKind.HOURS)
    rule = _rule(in_a, temp, operator="outside", min_value="40,5", max_value="80")
    assert rule.min_value == Decimal("40.5") and rule.threshold is None
    assert rule.action == ConditionRule.Action.ALERT_WORK_ORDER
    assert AuditEvent.objects.filter(target_id=str(rule.pk), action="create").exists()


def test_database_checks_back_the_scope(in_a, temp):
    with pytest.raises(IntegrityError), transaction.atomic():
        ConditionRule.objects.create(
            meter=temp,
            asset_class=in_a.asset.asset_class,
            meter_kind="temperature",
            operator="gt",
            threshold=1,
        )


def test_update_rule_keeps_scope_and_audits(in_a, temp):
    rule = _rule(in_a, temp)
    rule = services.update_rule(membership=in_a.admin, rule=rule, threshold="90", consecutive=2)
    assert rule.threshold == Decimal("90") and rule.consecutive == 2
    assert AuditEvent.objects.filter(target_id=str(rule.pk), action="update").exists()
    with pytest.raises(ConditionError):
        services.update_rule(membership=in_a.admin, rule=rule, meter=None)


def test_rules_need_the_condition_module(in_a, temp):
    in_a.org.plan = Plan.objects.get(code="basic")
    in_a.org.save(update_fields=["plan"])
    with pytest.raises(ModuleNotIncludedError):
        _rule(in_a, temp)


# --- Evaluación y alertas (CBM-04/05) -----------------------------------------------------


@pytest.mark.parametrize(
    "rule_kwargs,values,expected",
    [
        ({"operator": "gt", "threshold": "80"}, ["80", "80.001"], [False, True]),
        ({"operator": "lt", "threshold": "2"}, ["2", "1.9"], [False, True]),
        ({"operator": "outside", "min_value": "40", "max_value": "80"}, ["40", "39"], [False, True]),
        ({"operator": "outside", "min_value": "40", "max_value": "80"}, ["80", "81"], [False, True]),
    ],
)
def test_operators(in_a, temp, rule_kwargs, values, expected):
    rule = _rule(in_a, temp, action="alert", **rule_kwargs)
    result = []
    for hour, value in enumerate(values):
        read(temp, value, hours=hour)
        result.append(_state(rule, temp).status == "alarm")
    assert result == expected


def test_rate_uses_change_per_day_against_previous_reading(in_a, temp):
    rule = _rule(in_a, temp, operator="rate", threshold="10", action="alert")
    read(temp, "50")  # sin lectura anterior: no evalúa
    assert _state(rule, temp).status == "normal"
    read(temp, "54", hours=12)  # 4 en medio día = 8/día
    assert _state(rule, temp).status == "normal"
    read(temp, "48", hours=24)  # -6 en medio día = 12/día (valor absoluto)
    assert _state(rule, temp).status == "alarm"


def test_alert_and_predictive_work_order_on_crossing(in_a, temp, django_capture_on_commit_callbacks):
    rule = _rule(in_a, temp, work_order_instructions="Revisar lubricación.")
    with django_capture_on_commit_callbacks(execute=True):
        reading = read(temp, "85")
    alert = ConditionAlert.objects.get()
    assert (alert.rule, alert.meter, alert.asset, alert.reading) == (rule, temp, in_a.asset, reading)
    assert alert.status == Status.OPEN and alert.value == Decimal("85")
    wo = alert.work_order
    assert wo.work_type == WorkOrder.Type.PREDICTIVE and wo.status == WorkOrder.Status.PLANNED
    assert wo.title == "Condición: Rodamiento en CMP-01"
    assert "Revisar lubricación." in wo.description
    assert wo.priority == WorkOrder.Priority.NORMAL  # según criticidad (activo operativo)
    # Aviso a quienes gestionan OT (el admin), no al técnico ni al lector.
    notes = Notification.objects.filter(kind=Kind.CONDITION_ALERT)
    assert {n.recipient_id for n in notes} == {in_a.admin.user_id}
    assert notes.get().work_order == wo
    assert AuditEvent.objects.filter(target_id=str(alert.pk), action="create", actor__isnull=True).exists()


def test_work_order_description_uses_organization_timezone(in_a, temp):
    # Fuera de una petición (importación, tareas, seed_demo) no hay zona activada: la fecha de
    # la lectura va igual en la zona de la organización (BASE 08:00 UTC = 05:00 en Santiago).
    _rule(in_a, temp)
    read(temp, "85")
    assert "el 01-10-2026 05:00 fuera de rango" in ConditionAlert.objects.get().work_order.description


def test_rule_priority_and_title_override(in_a, temp):
    _rule(in_a, temp, work_order_title="Vibración alta", work_order_priority=1)
    read(temp, "85")
    wo = ConditionAlert.objects.get().work_order
    assert (wo.title, wo.priority) == ("Vibración alta", 1)


def test_alert_only_rule_creates_no_work_order(in_a, temp):
    _rule(in_a, temp, action="alert")
    read(temp, "85")
    assert ConditionAlert.objects.get().work_order is None
    assert not WorkOrder.objects.filter(work_type="predictive").exists()


def test_consecutive_readings_filter_noise(in_a, temp):
    rule = _rule(in_a, temp, consecutive=3)
    read(temp, "85")
    read(temp, "86", hours=1)
    read(temp, "70", hours=2)  # vuelve a rango: reinicia el contador
    read(temp, "85", hours=3)
    read(temp, "86", hours=4)
    assert not ConditionAlert.objects.exists()
    assert _state(rule, temp).consecutive_count == 2
    read(temp, "87", hours=5)
    assert ConditionAlert.objects.count() == 1


def test_hysteresis_one_alert_and_one_work_order_per_episode(in_a, temp):
    rule = _rule(in_a, temp)
    read(temp, "85")
    read(temp, "90", hours=1)
    read(temp, "95", hours=2)
    alert = ConditionAlert.objects.get()
    assert (alert.value, alert.last_value) == (Decimal("85"), Decimal("95"))
    assert WorkOrder.objects.filter(work_type="predictive").count() == 1
    state = _state(rule, temp)
    assert state.status == "alarm" and state.open_alert == alert

    read(temp, "70", hours=3)  # normaliza: la OT sigue su curso
    alert.refresh_from_db()
    assert alert.status == Status.NORMALIZED and alert.last_value == Decimal("70")
    assert alert.work_order.status == WorkOrder.Status.PLANNED
    assert _state(rule, temp).status == "normal"

    # Episodio nuevo con la OT anterior abierta: alerta nueva enlazada a la misma OT.
    read(temp, "88", hours=4)
    second = ConditionAlert.objects.exclude(pk=alert.pk).get()
    assert second.work_order == alert.work_order
    assert WorkOrder.objects.filter(work_type="predictive").count() == 1


def test_closing_work_order_closes_its_alerts(in_a, temp):
    _rule(in_a, temp)
    read(temp, "85")
    read(temp, "70", hours=1)
    read(temp, "86", hours=2)
    wo = WorkOrder.objects.get(work_type="predictive")
    transition(work_order=wo, to_status="in_progress", membership=in_a.admin)
    transition(work_order=wo, to_status="completed", membership=in_a.admin)
    wo.refresh_from_db()
    assert wo.status == WorkOrder.Status.CLOSED
    assert set(ConditionAlert.objects.values_list("status", flat=True)) == {Status.CLOSED}
    # Sigue en alarma: no hay otra alerta hasta que normalice y vuelva a cruzar.
    read(temp, "90", hours=3)
    assert ConditionAlert.objects.count() == 2
    read(temp, "60", hours=4)
    read(temp, "91", hours=5)
    assert ConditionAlert.objects.count() == 3
    # La OT anterior está cerrada: el episodio nuevo crea otra.
    assert WorkOrder.objects.filter(work_type="predictive").count() == 2


def test_cancelling_work_order_closes_its_alert(in_a, temp):
    _rule(in_a, temp)
    read(temp, "85")
    wo = WorkOrder.objects.get(work_type="predictive")
    transition(work_order=wo, to_status="cancelled", membership=in_a.admin, comment="Falsa alarma")
    alert = ConditionAlert.objects.get()
    assert alert.status == Status.CLOSED and alert.closed_at is not None
    assert AuditEvent.objects.filter(target_id=str(alert.pk), action="transition").exists()


def test_acknowledge_alert(in_a, temp):
    _rule(in_a, temp, action="alert")
    read(temp, "85")
    alert = services.acknowledge_alert(membership=in_a.admin, alert=ConditionAlert.objects.get())
    assert alert.status == Status.ACKNOWLEDGED and alert.acknowledged_by == in_a.admin.user
    assert services.acknowledge_alert(membership=in_a.admin, alert=alert).status == Status.ACKNOWLEDGED
    read(temp, "70", hours=1)
    alert.refresh_from_db()
    assert alert.status == Status.NORMALIZED
    with pytest.raises(ConditionError):
        services.acknowledge_alert(membership=in_a.admin, alert=alert)


def test_historical_readings_do_not_alert(in_a, temp):
    rule = _rule(in_a, temp)
    read(temp, "50", hours=10)
    read(temp, "99", hours=5)  # anterior a la vigente
    assert not ConditionAlert.objects.exists()
    assert _state(rule, temp).last_value == Decimal("50")


def test_resending_a_reading_with_client_uuid_is_idempotent(in_a, temp):
    _rule(in_a, temp)
    key = uuid.uuid4()
    read(temp, "85", client_uuid=key)
    read(temp, "85", client_uuid=key)
    assert MeterReading.objects.count() == 1
    assert ConditionAlert.objects.count() == 1
    # Reevaluar la misma lectura tampoco duplica.
    assert services.evaluate_reading(reading=MeterReading.objects.get()) == []
    assert ConditionAlert.objects.count() == 1


def test_class_rule_applies_to_every_asset_of_the_class_with_that_variable(in_a, temp):
    rule = _rule(
        in_a, asset_class=in_a.asset.asset_class, meter_kind=MeterKind.TEMPERATURE, meter_name="Rodamiento"
    )
    other_asset = Asset.objects.create(code="CMP-02", name="Compresor 2", asset_class=in_a.asset.asset_class)
    later = variable(other_asset)  # creada después de la regla
    different_name = variable(other_asset, name="Carcasa")
    read(temp, "85")
    read(later, "90")
    read(different_name, "99")
    assert set(ConditionAlert.objects.values_list("meter_id", flat=True)) == {temp.pk, later.pk}
    assert set(ConditionAlert.objects.values_list("rule_id", flat=True)) == {rule.pk}


def test_meter_rule_and_class_rule_both_apply(in_a, temp):
    _rule(in_a, temp, threshold="80", action="alert")
    _rule(
        in_a,
        asset_class=in_a.asset.asset_class,
        meter_kind=MeterKind.TEMPERATURE,
        meter_name="Rodamiento",
        threshold="90",
        action="alert",
    )
    read(temp, "85")
    assert ConditionAlert.objects.count() == 1
    read(temp, "95", hours=1)
    assert ConditionAlert.objects.count() == 2


def test_without_module_readings_are_saved_but_not_evaluated(in_a, temp):
    rule = _rule(in_a, temp)
    in_a.org.plan = Plan.objects.get(code="basic")
    in_a.org.save(update_fields=["plan"])
    read(temp, "85")
    assert MeterReading.objects.count() == 1
    assert not ConditionAlert.objects.exists()
    assert not ConditionState.objects.filter(rule=rule).exists()


def test_turning_module_back_on_starts_in_normal(in_a, temp):
    rule = _rule(in_a, temp, action="alert")
    read(temp, "85")
    alert = ConditionAlert.objects.get()
    in_a.org.plan = Plan.objects.get(code="basic")
    in_a.org.save(update_fields=["plan"])
    read(temp, "60", hours=1)  # sin evaluar
    in_a.org.plan = Plan.objects.get(code="pro")
    in_a.org.save(update_fields=["plan"])
    read(temp, "86", hours=2)
    alert.refresh_from_db()
    assert alert.status == Status.NORMALIZED  # el episodio anterior se dio por terminado
    assert ConditionAlert.objects.filter(status=Status.OPEN).count() == 1
    assert _state(rule, temp).status == "alarm"


def test_deactivating_a_rule_normalizes_and_stops_evaluation(in_a, temp):
    rule = _rule(in_a, temp, action="alert")
    read(temp, "85")
    services.set_rule_active(membership=in_a.admin, rule=rule, is_active=False)
    assert ConditionAlert.objects.get().status == Status.NORMALIZED
    read(temp, "95", hours=1)
    assert ConditionAlert.objects.count() == 1
    services.set_rule_active(membership=in_a.admin, rule=rule, is_active=True)
    read(temp, "96", hours=2)
    assert ConditionAlert.objects.filter(status=Status.OPEN).count() == 1


def test_usage_meters_are_not_evaluated(in_a):
    hours = Meter.objects.create(asset=in_a.asset, kind=MeterKind.HOURS)
    reading, _ = record_meter_reading(meter=hours, value=10)
    assert services.evaluate_reading(reading=reading) == []
