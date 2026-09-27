"""Checklist en OT no preventivas, fotos por ítem y firma al completar (WO-03 CA2–CA4)."""

import base64
import io
from decimal import Decimal

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from PIL import Image

from apps.assets.models import Asset, AssetClass
from apps.attachments.models import Attachment, Purpose, TargetType
from apps.attachments.selectors import BYTES_PER_MB
from apps.audit.models import AuditEvent
from apps.core.tenancy import tenant_context
from apps.maintenance.models import ItemType, PlanTaskItem
from apps.maintenance.services import assign_plan, create_plan, start_draft
from apps.organizations.models import Plan
from apps.work.forms import SIGNATURE_PREFIX
from apps.work.models import WorkOrder
from apps.work.selectors import checklist_plan_versions
from apps.work.services import (
    WorkOrderError,
    add_checklist_item,
    add_checklist_photo,
    answer_checklist_item,
    apply_plan_checklist,
    complete_work_order,
    create_work_order,
    report_failure,
    transition,
)
from tests.conftest import login, tiny_jpeg_bytes


@pytest.fixture(autouse=True)
def _media(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path


def _corrective(tenant):
    return report_failure(
        membership=tenant.viewer, asset=tenant.asset, description="Pierde aire", asset_stopped=False
    ).work_order


def _photo(name="foto.jpg"):
    return SimpleUploadedFile(name, tiny_jpeg_bytes(), content_type="image/jpeg")


def _signature_png(color=(11, 46, 79, 255)) -> bytes:
    """Firma como la deja el lienzo: PNG (aquí con fondo transparente, el peor caso)."""
    image = Image.new("RGBA", (300, 120), (0, 0, 0, 0))
    for x in range(40, 260):
        image.putpixel((x, 60), color)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _signature_data_url() -> str:
    return SIGNATURE_PREFIX + base64.b64encode(_signature_png()).decode()


# --- Aplicar el checklist de un plan -------------------------------------------------


def test_apply_plan_copies_items_like_preventive_generation(in_a):
    wo = _corrective(in_a)
    version = in_a.plan.current_version

    items = apply_plan_checklist(work_order=wo, plan_version=version, membership=in_a.admin)

    plan_items = list(version.items.all())
    assert [(i.text, i.item_type, i.unit, i.min_value, i.max_value, i.required) for i in items] == [
        (p.text, p.item_type, p.unit, p.min_value, p.max_value, p.required) for p in plan_items
    ]
    assert [i.order for i in wo.checklist.all()] == [1, 2]
    assert AuditEvent.objects.filter(target_id=wo.pk, changes__has_key="checklist_applied").exists()


def test_applied_checklist_is_a_snapshot(in_a):
    wo = _corrective(in_a)
    apply_plan_checklist(work_order=wo, plan_version=in_a.plan.current_version, membership=in_a.admin)
    draft = start_draft(plan=in_a.plan)
    PlanTaskItem.objects.filter(version=draft).update(text="Cambiado")

    assert list(wo.checklist.values_list("text", flat=True)) == ["Purgar", "Presión"]


def test_apply_appends_after_existing_items(in_a):
    wo = _corrective(in_a)
    add_checklist_item(work_order=wo, membership=in_a.admin, text="Revisar correa")
    apply_plan_checklist(work_order=wo, plan_version=in_a.plan.current_version, membership=in_a.admin)
    assert list(wo.checklist.values_list("order", "text")) == [
        (1, "Revisar correa"),
        (2, "Purgar"),
        (3, "Presión"),
    ]


def test_applying_the_same_plan_twice_is_rejected(in_a):
    wo = _corrective(in_a)
    version = in_a.plan.current_version
    apply_plan_checklist(work_order=wo, plan_version=version, membership=in_a.admin)
    with pytest.raises(WorkOrderError, match="ya está aplicado"):
        apply_plan_checklist(work_order=wo, plan_version=version, membership=in_a.admin)
    assert wo.checklist.count() == 2


def test_draft_version_cannot_be_applied(in_a):
    wo = _corrective(in_a)
    draft = start_draft(plan=in_a.plan)
    with pytest.raises(WorkOrderError, match="publicada"):
        apply_plan_checklist(work_order=wo, plan_version=draft, membership=in_a.admin)


@pytest.mark.parametrize("status", ["completed", "closed", "cancelled"])
def test_checklist_is_locked_once_the_work_order_is_done(in_a, status):
    wo = _corrective(in_a)
    WorkOrder.objects.filter(pk=wo.pk).update(status=status)
    wo.refresh_from_db()
    with pytest.raises(WorkOrderError, match="no admite cambios"):
        apply_plan_checklist(work_order=wo, plan_version=in_a.plan.current_version, membership=in_a.admin)
    with pytest.raises(WorkOrderError, match="no admite cambios"):
        add_checklist_item(work_order=wo, membership=in_a.admin, text="x")


def test_preventive_checklist_cannot_be_changed(in_a):
    wo = create_work_order(
        work_type="preventive", title="PM", asset=in_a.asset, checklist=in_a.plan.items.all()
    )
    with pytest.raises(WorkOrderError, match="preventiva"):
        add_checklist_item(work_order=wo, membership=in_a.admin, text="Extra")


def test_suggested_plans_come_first(in_a):
    other_class = AssetClass.objects.create(code="BMB", name="Bomba")
    other = create_plan(
        code="AAA",
        name="Aaa bomba",
        asset_class=other_class,
        interval_days=30,
        items=[{"text": "Cebar", "item_type": ItemType.CHECK}],
    )
    wo = _corrective(in_a)
    versions = list(checklist_plan_versions(work_order=wo))
    assert [(v.plan_id, v.suggested) for v in versions] == [(in_a.plan.pk, True), (other.pk, False)]

    # También se sugiere un plan asignado al activo aunque sea de otra clase.
    pump = Asset.objects.create(code="BMB-01", name="Bomba 1", asset_class=other_class)
    assign_plan(plan=in_a.plan, asset=pump)
    wo_pump = create_work_order(work_type="corrective", title="x", asset=pump)
    assert all(v.suggested for v in checklist_plan_versions(work_order=wo_pump))


# --- Ítems propios -------------------------------------------------------------------


def test_add_measurement_item(in_a):
    wo = _corrective(in_a)
    item = add_checklist_item(
        work_order=wo,
        membership=in_a.admin,
        text="  Corriente del motor ",
        item_type=ItemType.MEASUREMENT,
        unit="A",
        min_value="10",
        max_value="14.5",
        required=False,
    )
    assert (item.order, item.text, item.unit, item.min_value, item.max_value, item.required) == (
        1,
        "Corriente del motor",
        "A",
        Decimal("10"),
        Decimal("14.5"),
        False,
    )


def test_check_item_drops_range_and_unit(in_a):
    wo = _corrective(in_a)
    item = add_checklist_item(
        work_order=wo, membership=in_a.admin, text="Apretar pernos", unit="Nm", min_value=1, max_value=2
    )
    assert (item.unit, item.min_value, item.max_value) == ("", None, None)


@pytest.mark.parametrize(
    "kwargs,message",
    [
        ({"text": "   "}, "Escribe la tarea"),
        ({"text": "x", "item_type": "video"}, "Tipo de ítem inválido"),
        ({"text": "x", "item_type": ItemType.MEASUREMENT, "min_value": 5, "max_value": 1}, "mínimo"),
    ],
)
def test_add_item_validation(in_a, kwargs, message):
    with pytest.raises(WorkOrderError, match=message):
        add_checklist_item(work_order=_corrective(in_a), membership=in_a.admin, **kwargs)


def test_duplicate_item_text_is_rejected(in_a):
    wo = _corrective(in_a)
    add_checklist_item(work_order=wo, membership=in_a.admin, text="Revisar correa")
    with pytest.raises(WorkOrderError, match="ya tiene"):
        add_checklist_item(work_order=wo, membership=in_a.admin, text="Revisar correa")


def test_required_items_added_to_a_corrective_block_completion(in_a):
    """La regla existente (WO-03 CA4) se aplica sin cambios a ítems agregados."""
    wo = _corrective(in_a)
    add_checklist_item(work_order=wo, membership=in_a.admin, text="Probar en carga")
    transition(work_order=wo, to_status="in_progress", membership=in_a.tech)
    with pytest.raises(WorkOrderError, match="Faltan 1"):
        complete_work_order(work_order=wo, membership=in_a.tech, labor_minutes=10)

    answer_checklist_item(item=wo.checklist.get(), membership=in_a.tech, result="ok")
    complete_work_order(work_order=wo, membership=in_a.tech, labor_minutes=10)
    wo.refresh_from_db()
    assert wo.status == "closed"


# --- Fotos por ítem (WO-03 CA2) ------------------------------------------------------


def test_photo_is_attached_to_the_item_and_compressed(in_a):
    wo = _corrective(in_a)
    item = add_checklist_item(work_order=wo, membership=in_a.admin, text="Correa")
    buffer = io.BytesIO()
    Image.new("RGB", (3000, 1500), color="blue").save(buffer, format="JPEG")
    photo = SimpleUploadedFile("correa.jpg", buffer.getvalue(), content_type="image/jpeg")

    attachment = add_checklist_photo(item=item, membership=in_a.tech, uploaded_file=photo)

    assert (attachment.target_type, attachment.target_id) == (TargetType.CHECKLIST_ITEM, item.pk)
    assert attachment.purpose == Purpose.PHOTO
    assert attachment.width == 2000


def test_photo_is_idempotent_by_client_uuid(in_a):
    import uuid

    item = add_checklist_item(work_order=_corrective(in_a), membership=in_a.admin, text="Correa")
    key = uuid.uuid4()
    first = add_checklist_photo(item=item, membership=in_a.tech, uploaded_file=_photo(), client_uuid=key)
    again = add_checklist_photo(item=item, membership=in_a.tech, uploaded_file=_photo(), client_uuid=key)
    assert first.pk == again.pk
    assert Attachment.objects.filter(target_id=item.pk).count() == 1


def test_photo_rejects_non_images_and_closed_work_orders(in_a):
    wo = _corrective(in_a)
    item = add_checklist_item(work_order=wo, membership=in_a.admin, text="Correa")
    pdf = SimpleUploadedFile("m.pdf", b"%PDF-1.4", content_type="application/pdf")
    with pytest.raises(WorkOrderError, match="imagen"):
        add_checklist_photo(item=item, membership=in_a.tech, uploaded_file=pdf)
    WorkOrder.objects.filter(pk=wo.pk).update(status="closed")
    item.work_order.refresh_from_db()
    with pytest.raises(WorkOrderError, match="no admite cambios"):
        add_checklist_photo(item=item, membership=in_a.tech, uploaded_file=_photo())


def test_photo_respects_the_storage_quota(in_a):
    item = add_checklist_item(work_order=_corrective(in_a), membership=in_a.admin, text="Correa")
    in_a.org.plan = Plan.objects.create(code="mini", name="Mini", max_storage_mb=1)
    in_a.org.save()
    Attachment.objects.create(
        target_type=TargetType.ASSET,
        target_id=in_a.asset.pk,
        file="org/x/relleno.bin",
        filename="relleno.bin",
        content_type="application/pdf",
        size_bytes=BYTES_PER_MB,
    )
    with pytest.raises(WorkOrderError, match="No hay espacio"):
        add_checklist_photo(item=item, membership=in_a.tech, uploaded_file=_photo())


def test_technician_uploads_photo_from_the_mobile_checklist(client, tenant_a):
    with tenant_context(tenant_a.org):
        wo = _corrective(tenant_a)
        item = add_checklist_item(work_order=wo, membership=tenant_a.admin, text="Correa")
    login(client, tenant_a.tech)
    url = reverse("checklist_photo_add", args=[wo.pk, item.pk])

    response = client.post(url, {"photo": _photo()}, HTTP_HX_REQUEST="true")

    assert response.status_code == 200
    with tenant_context(tenant_a.org):
        attachment = Attachment.objects.get(target_id=item.pk)
    html = response.content.decode()
    assert reverse("attachment", args=[attachment.pk]) in html
    assert 'class="thumb thumb--sm"' in html
    # La foto se sirve por la vista con verificación de alcance, nunca por MEDIA_URL.
    assert client.get(reverse("attachment", args=[attachment.pk])).status_code == 200
    detail = client.get(reverse("work_order_detail", args=[wo.pk])).content.decode()
    assert reverse("attachment", args=[attachment.pk]) in detail


def test_photo_upload_error_is_shown_on_the_item(client, tenant_a):
    with tenant_context(tenant_a.org):
        wo = _corrective(tenant_a)
        item = add_checklist_item(work_order=wo, membership=tenant_a.admin, text="Correa")
    login(client, tenant_a.tech)
    bad = SimpleUploadedFile("x.jpg", b"no es una imagen", content_type="image/jpeg")
    response = client.post(
        reverse("checklist_photo_add", args=[wo.pk, item.pk]), {"photo": bad}, HTTP_HX_REQUEST="true"
    )
    assert response.status_code == 200
    assert "alert--error" in response.content.decode()


# --- Firma al completar (WO-03 CA3) --------------------------------------------------


def test_signature_is_saved_as_an_opaque_image_of_the_work_order(in_a):
    wo = _corrective(in_a)
    signature = SimpleUploadedFile("firma.png", _signature_png(), content_type="image/png")

    complete_work_order(work_order=wo, membership=in_a.tech, labor_minutes=15, signature=signature)

    attachment = Attachment.objects.get(target_id=wo.pk, purpose=Purpose.SIGNATURE)
    assert attachment.target_type == TargetType.WORK_ORDER
    assert attachment.uploaded_by == in_a.tech.user
    stored = Image.open(attachment.file)
    # El fondo transparente queda blanco (no negro) al pasar a JPEG.
    assert min(stored.convert("L").getpixel((5, 5)), 255) > 240


def test_completion_without_signature_still_works(in_a):
    wo = _corrective(in_a)
    complete_work_order(work_order=wo, membership=in_a.tech, labor_minutes=15)
    assert not Attachment.objects.filter(purpose=Purpose.SIGNATURE).exists()


def test_complete_view_saves_signature_and_detail_shows_it(client, tenant_a):
    with tenant_context(tenant_a.org):
        wo = _corrective(tenant_a)
    login(client, tenant_a.tech)
    detail_url = reverse("work_order_detail", args=[wo.pk])
    page = client.get(detail_url).content.decode()
    assert "data-signature-pad" in page and "js/signature-pad.js" in page

    response = client.post(
        reverse("work_order_complete", args=[wo.pk]),
        {
            "labor_minutes": 20,
            "impact": "none",
            "notes": "Cambio de correa",
            "signature": _signature_data_url(),
        },
    )

    assert response.status_code == 302
    with tenant_context(tenant_a.org):
        signature = Attachment.objects.get(target_id=wo.pk, purpose=Purpose.SIGNATURE)
    response = client.get(detail_url)
    assert response.context["signature"] == signature
    assert signature not in response.context["attachments"]
    assert 'class="signature-image"' in response.content.decode()


def test_invalid_signature_data_is_a_form_error(client, tenant_a):
    with tenant_context(tenant_a.org):
        wo = _corrective(tenant_a)
    login(client, tenant_a.tech)
    response = client.post(
        reverse("work_order_complete", args=[wo.pk]),
        {"labor_minutes": 20, "impact": "none", "signature": "data:text/html;base64,PHNjcmlwdD4="},
    )
    assert response.status_code == 400
    assert "La firma no es válida" in response.content.decode()
    with tenant_context(tenant_a.org):
        assert WorkOrder.objects.get(pk=wo.pk).status == "requested"


# --- Pantalla "Agregar checklist" ----------------------------------------------------


def test_planner_applies_plan_and_adds_item_from_the_page(client, tenant_a):
    with tenant_context(tenant_a.org):
        wo = _corrective(tenant_a)
        version = tenant_a.plan.current_version
    login(client, tenant_a.admin)
    url = reverse("work_order_checklist", args=[wo.pk])

    page = client.get(url)
    assert page.status_code == 200
    assert "Sugeridos para este activo" in page.content.decode()

    response = client.post(url, {"action": "apply", "apply-plan_version": str(version.pk)})
    assert response.status_code == 302
    response = client.post(
        url,
        {
            "action": "item",
            "item-text": "Corriente",
            "item-item_type": "measurement",
            "item-unit": "A",
            "item-min_value": "10",
            "item-max_value": "14",
            "item-required": "on",
        },
    )
    assert response.status_code == 302
    with tenant_context(tenant_a.org):
        assert list(wo.checklist.values_list("text", flat=True)) == ["Purgar", "Presión", "Corriente"]

    detail = client.get(reverse("work_order_detail", args=[wo.pk])).content.decode()
    assert url in detail  # el botón para agregar más ítems sigue disponible


def test_checklist_page_shows_form_errors(client, tenant_a):
    with tenant_context(tenant_a.org):
        wo = _corrective(tenant_a)
    login(client, tenant_a.admin)
    response = client.post(
        reverse("work_order_checklist", args=[wo.pk]),
        {
            "action": "item",
            "item-text": "x",
            "item-item_type": "measurement",
            "item-min_value": "5",
            "item-max_value": "1",
        },
    )
    assert response.status_code == 400
    assert "El máximo no puede ser menor" in response.content.decode()


def test_checklist_page_redirects_when_not_editable(client, tenant_a):
    with tenant_context(tenant_a.org):
        wo = create_work_order(work_type="preventive", title="PM", asset=tenant_a.asset)
    login(client, tenant_a.admin)
    response = client.get(reverse("work_order_checklist", args=[wo.pk]))
    assert response.status_code == 302
    detail = client.get(reverse("work_order_detail", args=[wo.pk])).context
    assert detail["can_edit_checklist"] is False


# --- Aviso de falla con el cupo lleno (REQ-01) ---------------------------------------


def _fill_quota(tenant):
    tenant.org.plan = Plan.objects.create(code=f"full-{tenant.org.slug}", name="Lleno", max_storage_mb=1)
    tenant.org.save()
    with tenant_context(tenant.org):
        Attachment.objects.create(
            target_type=TargetType.ASSET,
            target_id=tenant.asset.pk,
            file="org/x/relleno.bin",
            filename="relleno.bin",
            content_type="application/pdf",
            size_bytes=BYTES_PER_MB,
        )


def test_failure_report_is_created_without_photo_when_quota_is_full(tenant_a):
    _fill_quota(tenant_a)
    with tenant_context(tenant_a.org):
        request = report_failure(
            membership=tenant_a.viewer,
            asset=tenant_a.asset,
            description="Falla",
            asset_stopped=True,
            photo=_photo(),
        )
        assert request.pk and request.work_order_id
        assert "sin la foto" in request.photo_warning
        assert not Attachment.objects.filter(target_id=request.work_order_id).exists()
        assert Asset.objects.get(pk=tenant_a.asset.pk).status == "stopped"


def test_failure_report_with_unreadable_photo_still_fails(tenant_a):
    with tenant_context(tenant_a.org), pytest.raises(WorkOrderError, match="No se pudo leer"):
        report_failure(
            membership=tenant_a.viewer,
            asset=tenant_a.asset,
            description="Falla",
            asset_stopped=False,
            photo=SimpleUploadedFile("x.jpg", b"basura", content_type="image/jpeg"),
        )


def test_report_view_warns_when_photo_did_not_fit(client, tenant_a):
    _fill_quota(tenant_a)
    login(client, tenant_a.viewer)
    response = client.post(
        reverse("asset_report", args=[tenant_a.asset.pk]),
        {"description": "Ruido fuerte", "photo": _photo()},
        follow=True,
    )
    html = response.content.decode()
    assert "Aviso enviado" in html
    assert "alert--warning" in html and "sin la foto" in html
    with tenant_context(tenant_a.org):
        assert WorkOrder.objects.filter(title="Ruido fuerte").exists()
