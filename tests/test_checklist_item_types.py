"""Tipos de ítem del checklist: texto, foto obligatoria y firma (PM-01 CA1, WO-03)."""

import base64
import io

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from PIL import Image

from apps.attachments.models import Attachment, Purpose
from apps.core.tenancy import tenant_context
from apps.maintenance import services as plan_services
from apps.maintenance.models import ItemType
from apps.maintenance.services import create_plan
from apps.work.forms import SIGNATURE_PREFIX
from apps.work.models import WorkOrderChecklistItem
from apps.work.services import (
    WorkOrderError,
    add_checklist_item,
    add_checklist_photo,
    answer_checklist_item,
    complete_work_order,
    create_work_order,
    remove_checklist_photo,
)
from tests.conftest import login, tiny_jpeg_bytes

Result = WorkOrderChecklistItem.Result


@pytest.fixture(autouse=True)
def _media(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path


def _photo():
    return SimpleUploadedFile("f.jpg", tiny_jpeg_bytes(), content_type="image/jpeg")


def _signature_file():
    image = Image.new("RGBA", (200, 80), (0, 0, 0, 0))
    for x in range(20, 180):
        image.putpixel((x, 40), (11, 46, 79, 255))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


ALL_TYPES = [
    {"text": "Estado general", "item_type": ItemType.CHECK},
    {"text": "Observaciones del operador", "item_type": ItemType.TEXT},
    {"text": "Foto del tablero", "item_type": ItemType.PHOTO, "required": False},
    {"text": "Firma del supervisor", "item_type": ItemType.SIGNATURE},
]


def _preventive(tenant):
    plan = create_plan(code="PT", name="Tipos", calendar_interval=30, items=ALL_TYPES)
    wo = create_work_order(
        work_type="preventive",
        title="x",
        asset=tenant.asset,
        plan_version=plan.current_version,
        checklist=list(plan.items.all()),
    )
    return plan, wo, {i.item_type: i for i in wo.checklist.all()}


def test_plan_versions_and_snapshots_keep_new_item_types(in_a):
    plan, wo, items = _preventive(in_a)
    assert [i.item_type for i in plan.items.all()] == ["check", "text", "photo", "signature"]
    assert set(items) == {"check", "text", "photo", "signature"}
    # «Foto obligatoria» siempre bloquea el cierre, aunque se haya pedido opcional.
    assert items["photo"].required is True


def test_draft_editor_accepts_new_types_and_forces_required_photo(in_a):
    draft = plan_services.start_draft(plan=in_a.plan)
    item = plan_services.add_draft_item(
        membership=in_a.admin, version=draft, text="Foto", item_type=ItemType.PHOTO, required=False,
        unit="bar", min_value=1,
    )  # fmt: skip
    assert (item.item_type, item.required, item.unit, item.min_value) == ("photo", True, "", None)
    item = plan_services.update_draft_item(
        membership=in_a.admin, item=item, text="Comentario", item_type=ItemType.TEXT, required=False
    )
    assert (item.item_type, item.required) == ("text", False)


def test_text_answer_is_saved_and_counts_as_answered(in_a):
    _plan, wo, items = _preventive(in_a)
    with pytest.raises(WorkOrderError):
        answer_checklist_item(item=items["text"], membership=in_a.tech, value_text="   ")
    item = answer_checklist_item(item=items["text"], membership=in_a.tech, value_text=" Ruido leve ")
    assert (item.value_text, item.result) == ("Ruido leve", Result.DONE)
    wo.refresh_from_db()
    assert wo.status == "in_progress"


def test_required_photo_blocks_completion_until_a_photo_exists(in_a):
    _plan, wo, items = _preventive(in_a)
    answer_checklist_item(item=items["check"], membership=in_a.tech, result="ok")
    answer_checklist_item(item=items["text"], membership=in_a.tech, value_text="Ok")
    answer_checklist_item(item=items["signature"], membership=in_a.tech, signature=_sig())
    with pytest.raises(WorkOrderError, match="1 ítems obligatorios"):
        complete_work_order(work_order=wo, membership=in_a.tech, labor_minutes=0)
    with pytest.raises(WorkOrderError):  # la foto no se responde con un botón
        answer_checklist_item(item=items["photo"], membership=in_a.tech, result="ok")

    photo = add_checklist_photo(item=items["photo"], membership=in_a.tech, uploaded_file=_photo())
    items["photo"].refresh_from_db()
    assert items["photo"].result == Result.DONE
    # Quitar la única foto la deja pendiente otra vez.
    remove_checklist_photo(item=items["photo"], photo=photo, membership=in_a.tech)
    items["photo"].refresh_from_db()
    assert items["photo"].result == ""

    add_checklist_photo(item=items["photo"], membership=in_a.tech, uploaded_file=_photo())
    wo = complete_work_order(work_order=wo, membership=in_a.tech, labor_minutes=0)
    assert wo.status in ("completed", "closed")


def _sig():
    return SimpleUploadedFile("firma.png", _signature_file(), content_type="image/png")


def test_signature_item_stores_a_signature_attachment(in_a):
    _plan, _wo, items = _preventive(in_a)
    with pytest.raises(WorkOrderError):
        answer_checklist_item(item=items["signature"], membership=in_a.tech)
    item = answer_checklist_item(item=items["signature"], membership=in_a.tech, signature=_sig())
    assert item.result == Result.DONE
    assert Attachment.objects.filter(target_id=item.pk, purpose=Purpose.SIGNATURE).count() == 1


def test_ad_hoc_items_accept_new_types(in_a):
    wo = create_work_order(work_type="corrective", title="x", asset=in_a.asset)
    item = add_checklist_item(
        work_order=wo, membership=in_a.admin, text="Foto", item_type=ItemType.PHOTO, required=False
    )
    assert item.required is True


def test_mobile_checklist_renders_and_saves_new_types(client, tenant_a):
    with tenant_context(tenant_a.org):
        _plan, wo, items = _preventive(tenant_a)
    login(client, tenant_a.tech)
    page = client.get(reverse("work_order_detail", args=[wo.pk])).content.decode()
    assert 'name="value_text"' in page and "data-signature-pad" in page and "Falta la foto" in page

    url = reverse("checklist_answer", args=[wo.pk, items["text"].pk])
    response = client.post(url, {"value_text": "Sin novedad"}, HTTP_HX_REQUEST="true")
    assert response.status_code == 200 and "Respondido" in response.content.decode()

    data_url = SIGNATURE_PREFIX + base64.b64encode(_signature_file()).decode()
    url = reverse("checklist_answer", args=[wo.pk, items["signature"].pk])
    response = client.post(url, {"signature": data_url}, HTTP_HX_REQUEST="true")
    assert "Firmado" in response.content.decode()
    with tenant_context(tenant_a.org):
        assert WorkOrderChecklistItem.objects.get(pk=items["signature"].pk).result == Result.DONE
