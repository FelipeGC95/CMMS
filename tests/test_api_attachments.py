"""API v1: fotos de OT y de ítems del checklist (WO-03 CA2, AST-06) con los servicios de la web."""

import uuid

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

from apps.attachments.models import Attachment
from apps.audit.models import AuditEvent
from apps.core.tenancy import tenant_context
from apps.maintenance.models import ItemType
from apps.work.models import WorkOrder, WorkOrderChecklistItem
from apps.work.services import (
    WorkOrderError,
    add_checklist_photo,
    add_work_order_photo,
    answer_checklist_item,
    create_work_order,
)
from tests.api_utils import api_client
from tests.conftest import pdf_bytes, tiny_jpeg_bytes

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _media(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path


def _jpeg(name="foto.jpg"):
    return SimpleUploadedFile(name, tiny_jpeg_bytes(), content_type="image/jpeg")


def _wo(tenant, work_type="preventive"):
    with tenant_context(tenant.org):
        return create_work_order(work_type=work_type, title="OT", asset=tenant.asset)


def _item(tenant, wo, item_type=ItemType.PHOTO, **kwargs):
    with tenant_context(tenant.org):
        return WorkOrderChecklistItem.objects.create(
            work_order=wo, text="Foto del tablero", item_type=item_type, required=True, **kwargs
        )


def _url(wo, suffix):
    return f"/api/v1/work-orders/{wo.pk}/{suffix}/"


def _upload(client, url, **data):
    return client.post(url, {"file": _jpeg(), **data}, format="multipart")


# --- Servicios ------------------------------------------------------------------------------


def test_work_order_photo_service_compresses_audits_and_is_idempotent(in_a):
    wo = create_work_order(work_type="corrective", title="OT", asset=in_a.asset)
    key = uuid.uuid4()
    photo = add_work_order_photo(work_order=wo, membership=in_a.tech, uploaded_file=_jpeg(), client_uuid=key)
    again = add_work_order_photo(work_order=wo, membership=in_a.tech, uploaded_file=_jpeg(), client_uuid=key)
    assert again.pk == photo.pk and Attachment.objects.count() == 1
    assert (photo.target_type, photo.target_id, photo.purpose) == ("work_order", wo.pk, "photo")
    assert photo.content_type == "image/jpeg" and photo.file.name.startswith(f"org/{in_a.org.pk}/")
    event = AuditEvent.objects.get(action=AuditEvent.Action.CREATE, target_id=str(photo.pk))
    assert event.changes["sha256"] == photo.sha256 and event.actor == in_a.tech.user


def test_work_order_photo_service_rejects_non_images_and_closed_orders(in_a):
    wo = create_work_order(work_type="corrective", title="OT", asset=in_a.asset)
    pdf = SimpleUploadedFile("a.pdf", pdf_bytes(["x"]), content_type="application/pdf")
    with pytest.raises(WorkOrderError):
        add_work_order_photo(work_order=wo, membership=in_a.tech, uploaded_file=pdf)
    WorkOrder.objects.filter(pk=wo.pk).update(status=WorkOrder.Status.COMPLETED)
    wo.refresh_from_db()
    with pytest.raises(WorkOrderError, match="ya no admite cambios"):
        add_work_order_photo(work_order=wo, membership=in_a.tech, uploaded_file=_jpeg())
    assert not Attachment.objects.exists()


def test_photo_item_answer_needs_the_photo_first(in_a):
    wo = create_work_order(work_type="preventive", title="OT", asset=in_a.asset)
    item = WorkOrderChecklistItem.objects.create(work_order=wo, text="Foto", item_type=ItemType.PHOTO)
    with pytest.raises(WorkOrderError, match="súbela primero"):
        answer_checklist_item(item=item, membership=in_a.tech, note="x")
    add_checklist_photo(item=item, membership=in_a.tech, uploaded_file=_jpeg())
    item = answer_checklist_item(item=item, membership=in_a.tech, note="Tablero limpio")
    item.refresh_from_db()
    assert (item.result, item.note) == ("done", "Tablero limpio")


# --- API ------------------------------------------------------------------------------------


def test_upload_work_order_photo_returns_metadata_without_url(tenant_a):
    wo = _wo(tenant_a, "corrective")
    response = _upload(api_client(tenant_a), _url(wo, "photos"))
    assert response.status_code == 201, response.content
    body = response.json()
    assert body["target_type"] == "work_order" and body["target_id"] == str(wo.pk)
    assert body["purpose"] == "photo" and body["content_type"] == "image/jpeg"
    assert body["filename"] == "foto.jpg" and body["size_bytes"] > 0
    assert not any("url" in key or "file" == key for key in body)
    assert "/media/" not in response.content.decode() and "org/" not in response.content.decode()


def test_upload_is_idempotent_by_client_uuid_and_conflicts_elsewhere(tenant_a):
    wo, other = _wo(tenant_a, "corrective"), _wo(tenant_a, "corrective")
    client, key = api_client(tenant_a), str(uuid.uuid4())
    first = _upload(client, _url(wo, "photos"), client_uuid=key)
    again = _upload(client, _url(wo, "photos"), client_uuid=key)
    assert (first.status_code, again.status_code) == (201, 200)
    assert first.json()["id"] == again.json()["id"]
    conflict = _upload(client, _url(other, "photos"), client_uuid=key)
    assert conflict.status_code == 409 and conflict.json()["error"]["code"] == "conflict"
    with tenant_context(tenant_a.org):
        assert Attachment.objects.count() == 1
        assert AuditEvent.objects.filter(target_id=first.json()["id"]).count() == 1


def test_upload_validation_errors_are_400(tenant_a):
    wo = _wo(tenant_a, "corrective")
    client = api_client(tenant_a)
    missing = client.post(_url(wo, "photos"), {}, format="multipart")
    assert missing.status_code == 400 and "file" in missing.json()["error"]["details"]
    pdf = SimpleUploadedFile("a.pdf", pdf_bytes(["x"]), content_type="application/pdf")
    response = client.post(_url(wo, "photos"), {"file": pdf}, format="multipart")
    assert response.status_code == 400 and "imagen" in str(response.json()["error"]["details"])
    fake = SimpleUploadedFile("x.jpg", b"not an image", content_type="image/jpeg")
    assert client.post(_url(wo, "photos"), {"file": fake}, format="multipart").status_code == 400
    as_json = client.post(_url(wo, "photos"), {"client_uuid": str(uuid.uuid4())}, format="json")
    assert as_json.status_code == 415
    with tenant_context(tenant_a.org):
        assert not Attachment.objects.exists()


def test_photo_item_is_answered_by_its_photo_through_the_api(tenant_a):
    wo = _wo(tenant_a)
    item = _item(tenant_a, wo)
    client = api_client(tenant_a)
    answer = _url(wo, f"checklist-items/{item.pk}/answer")
    # Sin foto: error claro y el ítem sigue pendiente.
    refused = client.post(answer, {"note": "ok"}, format="json")
    assert refused.status_code == 400
    assert "súbela primero" in str(refused.json()["error"]["details"])
    # Bloquea el cierre como en la web.
    assert client.post(_url(wo, "complete"), {"labor_minutes": 5}, format="json").status_code == 400

    uploaded = _upload(client, _url(wo, f"checklist-items/{item.pk}/photos"), client_uuid=str(uuid.uuid4()))
    assert uploaded.status_code == 201, uploaded.content
    assert (uploaded.json()["target_type"], uploaded.json()["target_id"]) == ("checklist_item", str(item.pk))
    with tenant_context(tenant_a.org):
        item.refresh_from_db()
        assert item.result == "done" and WorkOrder.objects.get(pk=wo.pk).status == "in_progress"

    body = client.post(answer, {"note": "Tablero limpio"}, format="json").json()
    assert (body["result"], body["note"]) == ("done", "Tablero limpio")
    assert client.post(_url(wo, "complete"), {"labor_minutes": 5}, format="json").status_code == 200
    # Reenviar la misma respuesta con la OT ya completada no cambia nada.
    assert client.post(answer, {"note": "Tablero limpio"}, format="json").status_code == 200
    assert client.post(answer, {"note": "otra"}, format="json").status_code == 400


def test_replayed_photo_after_completion_returns_the_same_photo(tenant_a):
    wo = _wo(tenant_a)
    item = _item(tenant_a, wo)
    client, key = api_client(tenant_a), str(uuid.uuid4())
    url = _url(wo, f"checklist-items/{item.pk}/photos")
    first = _upload(client, url, client_uuid=key)
    assert client.post(_url(wo, "complete"), {"labor_minutes": 5}, format="json").status_code == 200
    again = _upload(client, url, client_uuid=key)
    assert again.status_code == 200 and again.json()["id"] == first.json()["id"]
    assert _upload(client, url).status_code == 400  # una foto nueva ya no entra


def test_item_photo_of_another_work_order_is_404(tenant_a):
    wo, other = _wo(tenant_a), _wo(tenant_a)
    item = _item(tenant_a, other)
    client = api_client(tenant_a)
    assert _upload(client, _url(wo, f"checklist-items/{item.pk}/photos")).status_code == 404
    assert _upload(client, _url(wo, f"checklist-items/{uuid.uuid4()}/photos")).status_code == 404


def test_list_checklist_items_and_attachments(tenant_a):
    wo = _wo(tenant_a)
    photo_item = _item(tenant_a, wo)
    _item(tenant_a, wo, item_type=ItemType.CHECK)
    client = api_client(tenant_a)
    _upload(client, _url(wo, f"checklist-items/{photo_item.pk}/photos"))
    _upload(client, _url(wo, "photos"))
    items = client.get(_url(wo, "checklist-items")).json()["results"]
    assert [(row["item_type"], row["photo_count"]) for row in items] == [("photo", 1), ("check", 0)]
    attachments = client.get(_url(wo, "attachments")).json()
    assert attachments["count"] == 2
    assert {row["target_type"] for row in attachments["results"]} == {"checklist_item", "work_order"}
    # Solo lectura también puede listar.
    assert api_client(tenant_a, scope="read").get(_url(wo, "attachments")).status_code == 200
