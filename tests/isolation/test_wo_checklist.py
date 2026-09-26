"""Checklist, fotos por ítem y firma: la organización B no ve, edita ni referencia datos de A."""

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from apps.attachments.models import Attachment, Purpose
from apps.core.tenancy import tenant_context
from apps.maintenance.models import PlanVersion
from apps.work.selectors import checklist_plan_versions
from apps.work.services import (
    WorkOrderError,
    add_checklist_item,
    add_checklist_photo,
    apply_plan_checklist,
    complete_work_order,
    report_failure,
)
from tests.conftest import login, tiny_jpeg_bytes


@pytest.fixture(autouse=True)
def _media(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path


def _photo():
    return SimpleUploadedFile("f.jpg", tiny_jpeg_bytes(), content_type="image/jpeg")


def _wo_with_item(tenant):
    with tenant_context(tenant.org):
        wo = report_failure(
            membership=tenant.viewer, asset=tenant.asset, description="Falla", asset_stopped=False
        ).work_order
        item = add_checklist_item(work_order=wo, membership=tenant.admin, text="Revisar")
    return wo, item


def test_plan_version_of_b_cannot_be_applied_to_a(tenant_a, tenant_b):
    wo, _item = _wo_with_item(tenant_a)
    with tenant_context(tenant_b.org):
        version_b = PlanVersion.objects.get(plan=tenant_b.plan)
    with tenant_context(tenant_a.org):
        with pytest.raises(WorkOrderError):
            apply_plan_checklist(work_order=wo, plan_version=version_b, membership=tenant_a.admin)
        assert wo.checklist.count() == 1
        # Las opciones de la pantalla solo traen planes de A.
        assert {v.plan_id for v in checklist_plan_versions(work_order=wo)} == {tenant_a.plan.pk}


def test_apply_view_ignores_plan_version_of_b(client, tenant_a, tenant_b):
    wo, _item = _wo_with_item(tenant_a)
    login(client, tenant_a.admin)
    response = client.post(
        reverse("work_order_checklist", args=[wo.pk]),
        {"action": "apply", "apply-plan_version": str(tenant_b.plan.current_version_id)},
    )
    assert response.status_code == 400
    with tenant_context(tenant_a.org):
        assert wo.checklist.count() == 1


def test_b_cannot_open_or_edit_the_checklist_of_a(client, tenant_a, tenant_b):
    wo, _item = _wo_with_item(tenant_a)
    login(client, tenant_b.admin)
    url = reverse("work_order_checklist", args=[wo.pk])
    assert client.get(url).status_code == 404
    assert (
        client.post(url, {"action": "item", "item-text": "Intruso", "item-item_type": "check"}).status_code
        == 404
    )
    with tenant_context(tenant_a.org):
        assert list(wo.checklist.values_list("text", flat=True)) == ["Revisar"]


def test_b_cannot_attach_photos_to_items_of_a(client, tenant_a, tenant_b):
    wo, item = _wo_with_item(tenant_a)
    login(client, tenant_b.admin)
    response = client.post(reverse("checklist_photo_add", args=[wo.pk, item.pk]), {"photo": _photo()})
    assert response.status_code == 404
    assert not Attachment.all_tenants.filter(target_id=item.pk).exists()  # all_tenants: verificación global


def test_b_cannot_read_photos_or_signature_of_a(client, tenant_a, tenant_b):
    wo, item = _wo_with_item(tenant_a)
    with tenant_context(tenant_a.org):
        photo = add_checklist_photo(item=item, membership=tenant_a.tech, uploaded_file=_photo())
        from apps.work.services import answer_checklist_item

        answer_checklist_item(item=item, membership=tenant_a.tech, result="ok")
        complete_work_order(
            work_order=wo,
            membership=tenant_a.tech,
            labor_minutes=5,
            signature=SimpleUploadedFile("firma.png", tiny_jpeg_bytes(), content_type="image/jpeg"),
        )
        signature = Attachment.objects.get(target_id=wo.pk, purpose=Purpose.SIGNATURE)

    login(client, tenant_a.tech)
    assert client.get(reverse("attachment", args=[photo.pk])).status_code == 200
    assert client.get(reverse("attachment", args=[signature.pk])).status_code == 200

    login(client, tenant_b.admin)
    assert client.get(reverse("attachment", args=[photo.pk])).status_code == 404
    assert client.get(reverse("attachment", args=[signature.pk])).status_code == 404
