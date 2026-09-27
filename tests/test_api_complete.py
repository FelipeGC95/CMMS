"""API v1: cierre de OT y respuesta de checklist (WO-03, REQ-03) con las reglas de la web."""

import uuid
from datetime import timedelta
from decimal import Decimal

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.api.models import ApiIdempotencyKey
from apps.assets.models import Meter, MeterKind
from apps.assets.services import record_meter_reading
from apps.core.tenancy import tenant_context
from apps.maintenance.services import assign_plan, create_plan, generate_due_work_orders
from apps.work.models import FailureCode, FailureEvent, WorkOrder, WorkOrderChecklistItem, WorkOrderTransition
from apps.work.services import create_work_order
from tests.api_utils import api_client

pytestmark = pytest.mark.django_db


def _url(wo, suffix="complete"):
    return f"/api/v1/work-orders/{wo.pk}/{suffix}/"


def _wo(tenant, work_type="corrective", **kwargs):
    with tenant_context(tenant.org):
        return create_work_order(work_type=work_type, title="OT", asset=tenant.asset, **kwargs)


def _item(tenant, wo, item_type="check", **kwargs):
    with tenant_context(tenant.org):
        return WorkOrderChecklistItem.objects.create(
            work_order=wo, text="Revisar", item_type=item_type, required=True, **kwargs
        )


def _get(tenant, model, **filters):
    with tenant_context(tenant.org):
        return model.objects.get(**filters)


CORRECTIVE = {
    "labor_minutes": 30,
    "impact": "total_stop",
    "symptom": "Ruido",
    "notes": "Cambié el rodamiento",
}


def test_complete_corrective_records_failure(tenant_a):
    wo = _wo(tenant_a)
    response = api_client(tenant_a).post(_url(wo), CORRECTIVE, format="json")
    assert response.status_code == 200, response.content
    assert response.json()["status"] in ("completed", "closed")
    event = _get(tenant_a, FailureEvent, work_order=wo)
    assert (event.description, event.impact) == ("Ruido", "total_stop")
    assert _get(tenant_a, WorkOrder, pk=wo.pk).completion_notes == "Cambié el rodamiento"


@pytest.mark.parametrize(
    "payload,field",
    [
        ({"impact": "total_stop"}, "labor_minutes"),
        ({"labor_minutes": 10}, "impact"),  # correctiva: el impacto es obligatorio
        ({**CORRECTIVE, "impact": "boom"}, "impact"),
        (
            {
                **CORRECTIVE,
                "failure_started_at": "2026-09-02T10:00:00Z",
                "failure_ended_at": "2026-09-01T10:00Z",
            },
            "failure_ended_at",
        ),
    ],
)
def test_complete_validation_errors(tenant_a, payload, field):
    response = api_client(tenant_a).post(_url(_wo(tenant_a)), payload, format="json")
    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == "invalid" and field in error["details"]


def test_org_requiring_failure_details(tenant_a):
    tenant_a.org.require_failure_details = True
    tenant_a.org.save()
    wo = _wo(tenant_a)
    client = api_client(tenant_a)
    response = client.post(_url(wo), CORRECTIVE, format="json")
    assert response.status_code == 400
    assert {"failure_mode", "failure_cause"} <= set(response.json()["error"]["details"])
    with tenant_context(tenant_a.org):
        mode = FailureCode.objects.create(kind="mode", code="M1", name="Desgaste")
        cause = FailureCode.objects.create(kind="cause", code="C1", name="Lubricación")
    payload = {**CORRECTIVE, "failure_mode": str(mode.pk), "failure_cause": str(cause.pk)}
    assert client.post(_url(wo), payload, format="json").status_code == 200
    assert _get(tenant_a, FailureEvent, work_order=wo).failure_mode_id == mode.pk


def test_required_checklist_items_block_completion(tenant_a):
    wo = _wo(tenant_a, work_type="preventive")
    _item(tenant_a, wo)
    response = api_client(tenant_a).post(_url(wo), {"labor_minutes": 5}, format="json")
    assert response.status_code == 400
    assert "obligatorios" in str(response.json()["error"]["details"])


def test_meter_reading_required_when_plan_uses_meter(tenant_a):
    with tenant_context(tenant_a.org):
        meter = Meter.objects.create(asset=tenant_a.asset, kind=MeterKind.HOURS)
        record_meter_reading(meter=meter, value=Decimal(20), read_at=timezone.now() - timedelta(days=1))
        plan = create_plan(
            code="H",
            name="H",
            meter_kind="hours",
            meter_interval=250,
            items=[{"text": "x", "item_type": "check"}],
        )
        assign_plan(plan=plan, asset=tenant_a.asset, meter=meter, next_due_meter_value=Decimal(10))
        (wo,) = generate_due_work_orders()
        WorkOrderChecklistItem.objects.filter(work_order=wo).update(result="ok")
    client = api_client(tenant_a)
    response = client.post(_url(wo), {"labor_minutes": 5}, format="json")
    assert response.status_code == 400 and "meter_value" in response.json()["error"]["details"]
    assert (
        client.post(_url(wo), {"labor_minutes": 5, "meter_value": "22.5"}, format="json").status_code == 200
    )
    assert _get(tenant_a, WorkOrder, pk=wo.pk).meter_value_at_completion == Decimal("22.5")


def test_complete_is_idempotent_by_client_uuid(tenant_a):
    wo = _wo(tenant_a)
    client = api_client(tenant_a)
    key = str(uuid.uuid4())
    first = client.post(_url(wo), {**CORRECTIVE, "client_uuid": key}, format="json")
    again = client.post(_url(wo), {**CORRECTIVE, "client_uuid": key}, format="json")
    assert first.status_code == again.status_code == 200
    assert first.json()["completed_at"] == again.json()["completed_at"]
    with tenant_context(tenant_a.org):
        assert WorkOrderTransition.objects.filter(work_order=wo, to_status="completed").count() == 1
        assert FailureEvent.objects.filter(work_order=wo).count() == 1
        assert ApiIdempotencyKey.objects.get().target_id == wo.pk
    # Sin clave, repetir el cierre es un error de transición; la misma clave en otra OT, un conflicto.
    assert client.post(_url(wo), CORRECTIVE, format="json").status_code == 400
    other = client.post(_url(_wo(tenant_a)), {**CORRECTIVE, "client_uuid": key}, format="json")
    assert other.status_code == 409 and other.json()["error"]["code"] == "conflict"


def test_answer_check_measurement_and_text_items(tenant_a):
    wo = _wo(tenant_a, work_type="preventive")
    check = _item(tenant_a, wo)
    gauge = _item(tenant_a, wo, item_type="measurement", min_value=Decimal(1), max_value=Decimal(5))
    text = _item(tenant_a, wo, item_type="text")
    client = api_client(tenant_a)
    body = client.post(
        _url(wo, f"checklist-items/{check.pk}/answer"), {"result": "ok", "note": "bien"}
    ).json()
    assert (body["result"], body["note"]) == ("ok", "bien")
    body = client.post(_url(wo, f"checklist-items/{gauge.pk}/answer"), {"value_number": "7.5"}).json()
    assert (body["result"], body["value_number"]) == ("not_ok", "7.500")
    body = client.post(_url(wo, f"checklist-items/{text.pk}/answer"), {"value_text": "Limpio"}).json()
    assert (body["result"], body["value_text"]) == ("done", "Limpio")
    assert _get(tenant_a, WorkOrder, pk=wo.pk).status == "in_progress"
    assert client.post(_url(wo), {"labor_minutes": 5}, format="json").status_code == 200


def test_answer_validation_errors(tenant_a):
    wo = _wo(tenant_a, work_type="preventive")
    client = api_client(tenant_a)
    check, gauge = _item(tenant_a, wo), _item(tenant_a, wo, item_type="measurement")
    assert client.post(_url(wo, f"checklist-items/{check.pk}/answer"), {}).status_code == 400
    assert client.post(_url(wo, f"checklist-items/{check.pk}/answer"), {"result": "done"}).status_code == 400
    response = client.post(_url(wo, f"checklist-items/{gauge.pk}/answer"), {})
    assert response.status_code == 400 and response.json()["error"]["code"] == "invalid"
    assert (
        client.post(_url(wo, f"checklist-items/{uuid.uuid4()}/answer"), {"result": "ok"}).status_code == 404
    )


def test_same_answer_after_completion_is_idempotent(tenant_a):
    wo = _wo(tenant_a, work_type="preventive")
    item = _item(tenant_a, wo)
    client = api_client(tenant_a)
    url = _url(wo, f"checklist-items/{item.pk}/answer")
    assert client.post(url, {"result": "ok"}).status_code == 200
    assert client.post(_url(wo), {"labor_minutes": 5}, format="json").status_code == 200
    assert client.post(url, {"result": "ok"}).status_code == 200  # reenvío: sin cambios
    assert client.post(url, {"result": "not_ok"}).status_code == 400  # la OT ya no admite cambios


def test_answer_queries_do_not_grow_with_checklist(tenant_a):
    wo = _wo(tenant_a, work_type="preventive")
    first = _item(tenant_a, wo)
    client = api_client(tenant_a)
    url = _url(wo, f"checklist-items/{first.pk}/answer")
    client.post(url, {"result": "ok"})

    def count(result):
        with CaptureQueriesContext(connection) as ctx:
            assert client.post(url, {"result": result}).status_code == 200
        return len(ctx)

    few = count("na")
    for _i in range(10):
        _item(tenant_a, wo)
    assert count("ok") == few
