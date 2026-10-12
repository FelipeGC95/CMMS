"""El checklist alimenta variables (CBM-06, CBM-02 CA4): enlace en el plan, snapshot y lectura al completar.

Base real (PostgreSQL en el CI). Las lecturas pasan por `record_meter_reading`
y su evaluación de reglas de condición (CBM-04).
"""

import datetime
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.urls import reverse
from django.utils import timezone

from apps.assets.models import Meter, MeterKind, MeterReading
from apps.audit.models import AuditEvent
from apps.condition import services as condition
from apps.condition.models import ConditionAlert
from apps.core.tenancy import tenant_context
from apps.maintenance import services as plans
from apps.maintenance.models import ItemType
from apps.work.models import WorkOrder, WorkOrderChecklistItem
from apps.work.services import (
    answer_checklist_item,
    apply_plan_checklist,
    complete_work_order,
    create_work_order,
    record_checklist_readings,
    transition,
)
from tests.condition_utils import variable
from tests.conftest import login

pytestmark = pytest.mark.django_db

S = WorkOrder.Status


def _linked_plan(code="PMT", kind=MeterKind.TEMPERATURE, name="Rodamiento", unit="°C"):
    return plans.create_plan(
        code=code,
        name="Termografía",
        calendar_interval=30,
        items=[
            {
                "text": "Temperatura rodamiento",
                "item_type": ItemType.MEASUREMENT,
                "unit": unit,
                "min_value": Decimal("-10"),
                "max_value": Decimal("80"),
                "meter_kind": kind,
                "meter_name": name,
            },
            {
                "text": "Presión de aceite",
                "item_type": ItemType.MEASUREMENT,
                "unit": "bar",
                "required": False,
            },
            {"text": "Limpiar", "item_type": ItemType.CHECK, "required": False},
        ],
    )


def _work_order(tenant, plan):
    return create_work_order(
        work_type=WorkOrder.Type.PREVENTIVE,
        title="Termografía",
        asset=tenant.asset,
        plan_version=plan.current_version,
        checklist=list(plan.current_version.items.all()),
    )


def _items(wo):
    return {i.text: i for i in WorkOrderChecklistItem.objects.filter(work_order=wo)}


def _answer(tenant, item, value, answered_at=None):
    return answer_checklist_item(
        item=item, membership=tenant.tech, value_number=value, answered_at=answered_at
    )


def _complete(tenant, wo):
    return complete_work_order(work_order=wo, membership=tenant.tech, labor_minutes=0)


# --- Enlace en el plan (CA1, CA5) -------------------------------------------------------


def test_link_is_validated_and_copied_to_the_snapshot(in_a):
    plan = _linked_plan()
    linked = plan.current_version.items.get(text="Temperatura rodamiento")
    assert (linked.meter_kind, linked.meter_name) == (MeterKind.TEMPERATURE, "Rodamiento")
    assert linked.variable_label == "Temperatura (°C) · Rodamiento"
    items = _items(_work_order(in_a, plan))
    snap = items["Temperatura rodamiento"]
    assert (snap.meter_kind, snap.meter_name, snap.meter_reading_id) == ("temperature", "Rodamiento", None)
    assert items["Presión de aceite"].meter_kind == ""


def test_draft_item_link_rules(in_a):
    draft = plans.create_draft(membership=in_a.admin, plan=in_a.plan)
    add = {"membership": in_a.admin, "version": draft, "text": "T", "unit": "A"}
    item = plans.add_draft_item(
        item_type=ItemType.MEASUREMENT, meter_kind="current", meter_name=" Motor ", **add
    )
    assert (item.meter_kind, item.meter_name) == ("current", "Motor")
    with pytest.raises(ValidationError):  # un tipo de uso no es una variable
        plans.add_draft_item(item_type=ItemType.MEASUREMENT, meter_kind=MeterKind.HOURS, **add)
    with pytest.raises(ValidationError):  # solo ítems de medición
        plans.add_draft_item(item_type=ItemType.CHECK, meter_kind=MeterKind.TEMPERATURE, **add)
    with pytest.raises(ValidationError):  # nombre sin tipo
        plans.add_draft_item(item_type=ItemType.MEASUREMENT, meter_name="Motor", **add)
    item = plans.update_draft_item(
        membership=in_a.admin,
        item=item,
        text="T",
        item_type=ItemType.MEASUREMENT,
        meter_kind=MeterKind.VIBRATION,
        meter_name="Motor",
    )
    assert item.meter_kind == MeterKind.VIBRATION
    event = AuditEvent.objects.filter(target_id=str(item.pk), action="update").get()
    assert event.changes["meter_kind"] == ["current", "vibration"]


def test_published_version_is_immutable_and_link_goes_in_a_new_draft(in_a):
    published = in_a.plan.current_version
    item = published.items.get(text="Presión")
    with pytest.raises(ValidationError):
        plans.update_draft_item(
            membership=in_a.admin,
            item=item,
            text="Presión",
            item_type=ItemType.MEASUREMENT,
            meter_kind="pressure",
        )
    draft = plans.create_draft(membership=in_a.admin, plan=in_a.plan)
    copy = draft.items.get(text="Presión")
    plans.update_draft_item(
        membership=in_a.admin,
        item=copy,
        text="Presión",
        item_type=ItemType.MEASUREMENT,
        unit="bar",
        meter_kind="pressure",
        meter_name="Descarga",
    )
    plans.publish_version(version=draft, by=in_a.admin.user)
    item.refresh_from_db()
    assert item.meter_kind == ""  # la versión publicada no cambió (ADR-016)
    # Un borrador nuevo copia el enlace de la vigente.
    in_a.plan.refresh_from_db()
    next_draft = plans.create_draft(membership=in_a.admin, plan=in_a.plan)
    assert next_draft.items.get(text="Presión").meter_name == "Descarga"


def test_apply_plan_checklist_copies_the_link(in_a):
    plan = _linked_plan()
    wo = create_work_order(work_type=WorkOrder.Type.CORRECTIVE, title="Ruido", asset=in_a.asset)
    apply_plan_checklist(work_order=wo, plan_version=plan.current_version, membership=in_a.admin)
    assert _items(wo)["Temperatura rodamiento"].meter_name == "Rodamiento"


# --- Lectura al completar (CA2, CA3; CBM-02 CA4) -----------------------------------------


def test_completing_records_one_reading_per_linked_item(in_a):
    meter = variable(in_a.asset)  # ya existe: no se crea otra
    wo = _work_order(in_a, _linked_plan())
    # Creada ayer: la hora de respuesta informada no se acota a la creación.
    WorkOrder.objects.filter(pk=wo.pk).update(created_at=timezone.now() - datetime.timedelta(days=1))
    wo.refresh_from_db()
    items = _items(wo)
    answered = timezone.now() - datetime.timedelta(minutes=30)
    _answer(in_a, items["Temperatura rodamiento"], "-4,5", answered_at=answered)  # bajo cero es válido
    _answer(in_a, items["Presión de aceite"], "7")  # sin enlace: no registra
    wo = _complete(in_a, wo)
    reading = MeterReading.objects.get()
    assert reading.meter == meter and reading.value == Decimal("-4.5")
    assert reading.source == MeterReading.Source.WORK_ORDER and reading.work_order == wo
    assert reading.read_at == answered and reading.created_by == in_a.tech.user
    assert WorkOrderChecklistItem.objects.get(pk=items["Temperatura rodamiento"].pk).meter_reading == reading
    meter.refresh_from_db()
    assert meter.current_value == Decimal("-4.5")
    assert Meter.objects.filter(asset=in_a.asset).count() == 1


def test_missing_variable_is_created_with_the_item_unit(in_a):
    wo = _work_order(in_a, _linked_plan(kind=MeterKind.VARIABLE, name="Caudal", unit="l/min"))
    _answer(in_a, _items(wo)["Temperatura rodamiento"], "12")
    _complete(in_a, wo)
    meter = Meter.objects.get(asset=in_a.asset, kind=MeterKind.VARIABLE, name="Caudal")
    assert meter.unit == "l/min" and meter.is_cumulative is False
    assert meter.readings.get().value == Decimal("12")
    assert AuditEvent.objects.filter(target_id=str(meter.pk), action="create").exists()


def test_item_without_value_records_nothing(in_a):
    wo = _work_order(in_a, _linked_plan())
    # El ítem enlazado queda sin responder (se vuelve opcional para poder completar).
    WorkOrderChecklistItem.objects.filter(work_order=wo).update(required=False)
    _complete(in_a, wo)
    assert not MeterReading.objects.exists()
    assert not Meter.objects.filter(asset=in_a.asset).exists()


def test_reading_time_falls_back_to_completion(in_a):
    wo = _work_order(in_a, _linked_plan())
    WorkOrderChecklistItem.objects.filter(work_order=wo, text="Temperatura rodamiento").update(
        value_number=Decimal("50"), result="ok"
    )
    WorkOrderChecklistItem.objects.filter(work_order=wo).update(required=False)
    wo = _complete(in_a, wo)
    assert MeterReading.objects.get().read_at == wo.completed_at


def test_recompleting_is_idempotent(in_a):
    in_a.org.require_close_approval = True
    in_a.org.save(update_fields=["require_close_approval"])
    wo = _work_order(in_a, _linked_plan())
    _answer(in_a, _items(wo)["Temperatura rodamiento"], "40")
    wo = _complete(in_a, wo)
    assert wo.status == S.COMPLETED
    transition(work_order=wo, to_status=S.IN_PROGRESS, membership=in_a.admin)  # reabrir
    transition(work_order=wo, to_status=S.COMPLETED, membership=in_a.admin)
    assert record_checklist_readings(work_order=wo, membership=in_a.admin) == []
    assert MeterReading.objects.count() == 1


def test_location_work_order_records_nothing(in_a):
    from apps.organizations.models import Location

    site = Location.objects.create(code="S1", name="Planta", kind="site")
    wo = create_work_order(work_type=WorkOrder.Type.CORRECTIVE, title="Techo", location=site)
    WorkOrderChecklistItem.objects.create(
        work_order=wo, text="T", item_type="measurement", value_number=1, meter_kind="temperature"
    )
    assert record_checklist_readings(work_order=wo) == []


def test_inactive_variable_is_not_reactivated(in_a):
    meter = variable(in_a.asset)
    meter.is_active = False
    meter.save(update_fields=["is_active"])
    wo = _work_order(in_a, _linked_plan())
    _answer(in_a, _items(wo)["Temperatura rodamiento"], "40")
    _complete(in_a, wo)
    assert not MeterReading.objects.exists()


def test_reading_from_checklist_opens_condition_alert(in_a, django_capture_on_commit_callbacks):
    condition.create_rule(
        membership=in_a.admin,
        asset_class=in_a.asset.asset_class,
        meter_kind=MeterKind.TEMPERATURE,
        meter_name="Rodamiento",
        operator="gt",
        threshold="80",
    )
    wo = _work_order(in_a, _linked_plan())
    _answer(in_a, _items(wo)["Temperatura rodamiento"], "95")
    with django_capture_on_commit_callbacks(execute=True):
        wo = _complete(in_a, wo)
    alert = ConditionAlert.objects.get()  # variable creada sola y evaluada por la regla de clase
    assert alert.value == Decimal("95") and alert.reading.work_order == wo
    predictive = alert.work_order
    assert predictive.work_type == WorkOrder.Type.PREDICTIVE and predictive.pk != wo.pk
    assert predictive.status == S.PLANNED
    assert wo.status == S.CLOSED


# --- Pantallas -------------------------------------------------------------------------


def test_plan_editor_links_an_item_to_a_variable(client, tenant_a):
    with tenant_context(tenant_a.org):
        draft = plans.create_draft(membership=tenant_a.admin, plan=tenant_a.plan)
        item = draft.items.get(text="Presión")
    login(client, tenant_a.admin)
    url = reverse("plan_item_edit", args=[tenant_a.plan.pk, item.pk])
    page = client.get(url).content.decode()
    assert 'name="meter_kind"' in page and 'name="meter_name"' in page
    assert 'value="hours"' not in page  # solo tipos variable
    data = {"text": "Presión", "item_type": "measurement", "unit": "bar", "required": "on"}
    response = client.post(url, {**data, "meter_kind": "pressure", "meter_name": "Descarga"})
    assert response.status_code == 302
    with tenant_context(tenant_a.org):
        item.refresh_from_db()
        assert (item.meter_kind, item.meter_name) == ("pressure", "Descarga")
    page = client.get(reverse("plan_draft", args=[tenant_a.plan.pk])).content.decode()
    assert "Alimenta Presión (bar) · Descarga" in page
    # Un ítem que no es medición no puede alimentar una variable: error en el formulario.
    response = client.post(url, {**data, "item_type": "check", "meter_kind": "pressure"})
    assert response.status_code == 200 and "Solo un ítem de medición" in response.content.decode()


def test_work_order_shows_the_variable_and_the_recorded_reading(client, tenant_a):
    with tenant_context(tenant_a.org):
        wo = _work_order(tenant_a, _linked_plan())
        item = _items(wo)["Temperatura rodamiento"]
    login(client, tenant_a.tech)
    url = reverse("work_order_detail", args=[wo.pk])
    page = client.get(url).content.decode()
    assert "el valor queda como lectura de Temperatura (°C) · Rodamiento" in page
    with tenant_context(tenant_a.org):
        _answer(tenant_a, item, "40")
        _complete(tenant_a, wo)
    page = client.get(url).content.decode()
    assert "Lectura registrada en la variable Temperatura (°C) · Rodamiento" in page
