"""Hallazgo sin conexión (WO-06 CA2): `POST /field/api/work-orders/{id}/checklist-items/{item}/finding/`
(y su ruta anterior `…/checklist/{item}/finding/`)."""

import uuid

import pytest

from apps.api.models import ApiIdempotencyKey
from apps.assets.models import Asset
from apps.core.tenancy import tenant_context
from apps.work.models import WorkOrder, WorkOrderChecklistItem, WorkRequest
from apps.work.services import answer_checklist_item, create_work_order
from tests.api_utils import FIELD_API, field_client

pytestmark = pytest.mark.django_db

S = WorkOrder.Status


def _finding_wo(tenant, *, result="not_ok", item_type="check", **item_fields):
    """OT preventiva con un ítem respondido por el técnico (No OK por defecto)."""
    with tenant_context(tenant.org):
        wo = create_work_order(work_type="preventive", title="Mensual", asset=tenant.asset)
        item = WorkOrderChecklistItem.objects.create(
            work_order=wo, text="Revisar fugas", item_type=item_type, required=True, **item_fields
        )
        if result:
            answer_checklist_item(item=item, membership=tenant.tech, result=result, note="Gotea el sello")
        wo.refresh_from_db()
    return wo, item


def _url(wo, item, route="checklist-items"):
    return f"{FIELD_API}/work-orders/{wo.pk}/{route}/{item.pk}/finding/"


def _set_status(tenant, wo, status):
    with tenant_context(tenant.org):
        WorkOrder.objects.filter(pk=wo.pk).update(status=status)


def test_technician_reports_the_finding_as_a_request_and_corrective(tenant_a):
    wo, item = _finding_wo(tenant_a)
    body = {"description": "Cambiar sello", "asset_stopped": True, "client_uuid": str(uuid.uuid4())}
    response = field_client(tenant_a.tech).post(_url(wo, item), body, format="json")
    assert response.status_code == 201, response.json()
    data = response.json()
    with tenant_context(tenant_a.org):
        corrective = WorkOrder.objects.get(source_checklist_item=item)
        request = WorkRequest.objects.get(work_order=corrective)
        asset_status = Asset.objects.get(pk=tenant_a.asset.pk).status
        assert data == {
            "id": str(corrective.pk),
            "code": corrective.code,
            "status": S.REQUESTED,
            "item": str(item.pk),
            "request_number": request.number,
        }
        assert corrective.parent_work_order_id == wo.pk and corrective.work_type == "corrective"
        assert "Cambiar sello" in corrective.description and corrective.title == "Revisar fugas"
        assert request.asset_stopped and asset_status == Asset.Status.STOPPED


def test_manager_creates_the_corrective_directly(tenant_a):
    wo, item = _finding_wo(tenant_a)
    response = field_client(tenant_a.admin).post(_url(wo, item), {}, format="json")
    assert response.status_code == 201
    data = response.json()
    assert data["status"] == S.PLANNED and data["request_number"] is None
    with tenant_context(tenant_a.org):
        assert not WorkRequest.objects.exists()
        assert WorkOrder.objects.get(pk=data["id"]).source_checklist_item_id == item.pk


def test_one_corrective_per_item_even_with_another_client_uuid(tenant_a):
    wo, item = _finding_wo(tenant_a)
    client = field_client(tenant_a.tech)
    first = client.post(_url(wo, item), {"client_uuid": str(uuid.uuid4())}, format="json")
    second = client.post(_url(wo, item), {"client_uuid": str(uuid.uuid4())}, format="json")
    assert (first.status_code, second.status_code) == (201, 200)
    assert first.json()["id"] == second.json()["id"]
    with tenant_context(tenant_a.org):
        assert WorkOrder.objects.filter(source_checklist_item=item).count() == 1
        assert WorkRequest.objects.count() == 1


def test_resending_the_client_uuid_returns_the_corrective_even_after_completing(tenant_a):
    wo, item = _finding_wo(tenant_a)
    client = field_client(tenant_a.tech)
    body = {"client_uuid": str(uuid.uuid4())}
    created = client.post(_url(wo, item), body, format="json").json()
    # La respuesta se perdió, la OT se completó y la correctiva se cerró: el reintento no duplica.
    _set_status(tenant_a, wo, S.COMPLETED)
    with tenant_context(tenant_a.org):
        WorkOrder.objects.filter(pk=created["id"]).update(status=S.CANCELLED)
    again = client.post(_url(wo, item), body, format="json")
    assert again.status_code == 200 and again.json()["id"] == created["id"]
    with tenant_context(tenant_a.org):
        assert WorkOrder.objects.filter(source_checklist_item=item).count() == 1
        assert ApiIdempotencyKey.objects.filter(client_uuid=body["client_uuid"]).count() == 1


def test_client_uuid_used_for_another_item_or_action_is_a_conflict(tenant_a):
    wo, item = _finding_wo(tenant_a)
    with tenant_context(tenant_a.org):
        other = WorkOrderChecklistItem.objects.create(work_order=wo, text="Otro", item_type="check")
        answer_checklist_item(item=other, membership=tenant_a.tech, result="not_ok")
    client = field_client(tenant_a.tech)
    key = str(uuid.uuid4())
    assert client.post(_url(wo, item), {"client_uuid": key}, format="json").status_code == 201
    response = client.post(_url(wo, other), {"client_uuid": key}, format="json")
    assert response.status_code == 409 and response.json()["error"]["code"] == "conflict"
    used = str(uuid.uuid4())
    transition = {"to_status": "on_hold", "client_uuid": used}
    assert (
        client.post(f"{FIELD_API}/work-orders/{wo.pk}/transition/", transition, format="json").status_code
        == 200
    )
    response = client.post(_url(wo, other), {"client_uuid": used}, format="json")
    assert response.status_code == 409 and response.json()["error"]["code"] == "conflict"


@pytest.mark.parametrize(
    ("result", "item_type", "fields"),
    [("ok", "check", {}), ("na", "check", {}), ("", "check", {}), ("done", "text", {})],
)
def test_item_that_is_not_not_ok_is_rejected(tenant_a, result, item_type, fields):
    wo, item = _finding_wo(tenant_a, result="", item_type=item_type, **fields)
    if result:
        with tenant_context(tenant_a.org):
            WorkOrderChecklistItem.objects.filter(pk=item.pk).update(result=result)
    response = field_client(tenant_a.tech).post(_url(wo, item), {}, format="json")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid"
    with tenant_context(tenant_a.org):
        assert not WorkOrder.objects.filter(source_checklist_item=item).exists()


def test_measurement_out_of_range_is_a_finding(tenant_a):
    wo, item = _finding_wo(tenant_a, result="", item_type="measurement", unit="bar", min_value=6, max_value=8)
    with tenant_context(tenant_a.org):
        answer_checklist_item(item=item, membership=tenant_a.tech, value_number="9,5")
    response = field_client(tenant_a.tech).post(_url(wo, item), {}, format="json")
    assert response.status_code == 201
    with tenant_context(tenant_a.org):
        assert "(9,5 bar)" in WorkOrder.objects.get(pk=response.json()["id"]).description


def test_cancelled_work_order_is_a_conflict(tenant_a):
    wo, item = _finding_wo(tenant_a)
    _set_status(tenant_a, wo, S.CANCELLED)
    response = field_client(tenant_a.tech).post(
        _url(wo, item), {"client_uuid": str(uuid.uuid4())}, format="json"
    )
    assert response.status_code == 409 and response.json()["error"]["code"] == "cancelled"
    with tenant_context(tenant_a.org):
        assert not WorkOrder.objects.filter(source_checklist_item=item).exists()


@pytest.mark.parametrize("status", [S.COMPLETED, S.CLOSED])
def test_finding_reported_before_completing_is_accepted_after(tenant_a, status):
    """Un aviso rechazado se corrige y reenvía después de "Completar" (no detiene la OT), incluso
    con un client_uuid nuevo: como en la web, la OT terminada no impide la correctiva."""
    wo, item = _finding_wo(tenant_a)
    _set_status(tenant_a, wo, status)
    body = {"description": "Corregido", "client_uuid": str(uuid.uuid4())}
    response = field_client(tenant_a.tech).post(_url(wo, item), body, format="json")
    assert response.status_code == 201, response.json()
    with tenant_context(tenant_a.org):
        corrective = WorkOrder.objects.get(source_checklist_item=item)
        assert corrective.parent_work_order_id == wo.pk
        assert WorkOrder.objects.get(pk=wo.pk).status == status  # la OT de origen no cambia


def test_rejected_finding_is_resent_with_the_same_client_uuid(tenant_a):
    """Un 400 no guarda la clave: el mismo client_uuid, corregido, crea la correctiva."""
    wo, item = _finding_wo(tenant_a)
    client = field_client(tenant_a.tech)
    key = str(uuid.uuid4())
    rejected = client.post(_url(wo, item), {"description": "x" * 2001, "client_uuid": key}, format="json")
    assert rejected.status_code == 400
    _set_status(tenant_a, wo, S.COMPLETED)  # "Completar" se adelantó al hallazgo rechazado
    fixed = client.post(_url(wo, item), {"description": "Sello", "client_uuid": key}, format="json")
    assert fixed.status_code == 201
    again = client.post(_url(wo, item), {"description": "Sello", "client_uuid": key}, format="json")
    assert again.status_code == 200 and again.json()["id"] == fixed.json()["id"]


def test_legacy_route_is_an_alias(tenant_a):
    """Teléfonos con la versión anterior de la PWA envían a `checklist/{item}/finding/`."""
    wo, item = _finding_wo(tenant_a)
    client = field_client(tenant_a.tech)
    body = {"client_uuid": str(uuid.uuid4())}
    legacy = client.post(_url(wo, item, "checklist"), body, format="json")
    assert legacy.status_code == 201
    canonical = client.post(_url(wo, item), body, format="json")
    assert canonical.status_code == 200 and canonical.json() == legacy.json()


def test_unknown_item_is_404(tenant_a):
    wo, _item = _finding_wo(tenant_a)
    other_wo, other_item = _finding_wo(tenant_a)
    response = field_client(tenant_a.tech).post(_url(wo, other_item), {}, format="json")
    assert response.status_code == 404


def test_description_is_validated(tenant_a):
    wo, item = _finding_wo(tenant_a)
    response = field_client(tenant_a.tech).post(_url(wo, item), {"description": "x" * 2001}, format="json")
    assert response.status_code == 400 and "description" in response.json()["error"]["details"]


def test_snapshot_shows_the_open_finding_and_the_variable(tenant_a):
    wo, item = _finding_wo(tenant_a, meter_kind="temperature", meter_name="Rodamiento")
    client = field_client(tenant_a.tech)

    def snapshot_item():
        work_orders = client.get(f"{FIELD_API}/snapshot/").json()["work_orders"]
        row = next(row for row in work_orders if row["id"] == str(wo.pk))
        return row["checklist"][0]

    before = snapshot_item()
    assert before["finding_code"] is None and before["finding_request_number"] is None
    assert before["variable"] == "Temperatura (°C) · Rodamiento"
    created = client.post(_url(wo, item), {}, format="json").json()
    after = snapshot_item()
    assert after["finding_code"] == created["code"]
    assert after["finding_request_number"] == created["request_number"] is not None
    # Cancelada la correctiva, el ítem puede avisarse de nuevo (como en la web).
    with tenant_context(tenant_a.org):
        WorkOrder.objects.filter(pk=created["id"]).update(status=S.CANCELLED)
    assert snapshot_item()["finding_code"] is None
    assert snapshot_item()["finding_request_number"] is None


def test_snapshot_request_number_is_null_when_a_manager_created_the_corrective(tenant_a):
    wo, item = _finding_wo(tenant_a)
    field_client(tenant_a.admin).post(_url(wo, item), {}, format="json")
    work_orders = field_client(tenant_a.tech).get(f"{FIELD_API}/snapshot/").json()["work_orders"]
    row = next(row for row in work_orders if row["id"] == str(wo.pk))["checklist"][0]
    assert row["finding_code"] and row["finding_request_number"] is None
