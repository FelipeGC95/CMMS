"""Permisos del checklist en OT no preventivas, fotos por ítem y firma (ADR-013, WO-03).

Técnico: ejecuta, fotografía y firma, pero no agrega ítems. Lector (viewer): nada de eso.
"""

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from apps.accounts.permissions import PermissionDeniedError
from apps.attachments.models import Attachment, Purpose
from apps.core.tenancy import tenant_context
from apps.work.models import WorkOrder
from apps.work.services import (
    add_checklist_item,
    add_checklist_photo,
    apply_plan_checklist,
    report_failure,
)
from tests.conftest import login, tiny_jpeg_bytes
from tests.test_wo_checklist import _signature_data_url


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


@pytest.mark.parametrize("role", ["technician", "viewer", "external"])
def test_only_managers_add_items_in_the_service(tenant_a, role):
    wo, _item = _wo_with_item(tenant_a)
    member = tenant_a.tech
    member.role = role
    member.save()
    with tenant_context(tenant_a.org):
        with pytest.raises(PermissionDeniedError):
            add_checklist_item(work_order=wo, membership=member, text="Otro")
        with pytest.raises(PermissionDeniedError):
            apply_plan_checklist(work_order=wo, plan_version=tenant_a.plan.current_version, membership=member)


@pytest.mark.parametrize("role", ["planner", "supervisor"])
def test_planner_and_supervisor_add_items(tenant_a, role):
    wo, _item = _wo_with_item(tenant_a)
    member = tenant_a.tech
    member.role = role
    member.save()
    with tenant_context(tenant_a.org):
        add_checklist_item(work_order=wo, membership=member, text="Otro")
        assert wo.checklist.count() == 2


@pytest.mark.parametrize("member_name", ["tech", "viewer"])
def test_technician_and_viewer_cannot_use_the_checklist_page(client, tenant_a, member_name):
    wo, _item = _wo_with_item(tenant_a)
    login(client, getattr(tenant_a, member_name))
    url = reverse("work_order_checklist", args=[wo.pk])
    assert client.get(url).status_code == 403
    assert (
        client.post(url, {"action": "item", "item-text": "x", "item-item_type": "check"}).status_code == 403
    )
    detail = client.get(reverse("work_order_detail", args=[wo.pk]))
    assert detail.context["can_edit_checklist"] is False
    assert url not in detail.content.decode()


def test_technician_can_photograph_items(client, tenant_a):
    wo, item = _wo_with_item(tenant_a)
    login(client, tenant_a.tech)
    client.post(reverse("checklist_photo_add", args=[wo.pk, item.pk]), {"photo": _photo()})
    with tenant_context(tenant_a.org):
        assert Attachment.objects.filter(target_id=item.pk).count() == 1


def test_viewer_cannot_photograph_items(client, tenant_a):
    wo, item = _wo_with_item(tenant_a)
    with tenant_context(tenant_a.org), pytest.raises(PermissionDeniedError):
        add_checklist_photo(item=item, membership=tenant_a.viewer, uploaded_file=_photo())
    login(client, tenant_a.viewer)
    response = client.post(reverse("checklist_photo_add", args=[wo.pk, item.pk]), {"photo": _photo()})
    assert response.status_code == 403
    with tenant_context(tenant_a.org):
        assert not Attachment.objects.filter(target_id=item.pk).exists()
    # Tampoco ve el botón de foto ni el lienzo de firma.
    page = client.get(reverse("work_order_detail", args=[wo.pk])).content.decode()
    assert "photo-upload" not in page and "data-signature-pad" not in page


def test_technician_can_sign_and_viewer_cannot(client, tenant_a):
    wo, item = _wo_with_item(tenant_a)
    data = {"labor_minutes": 5, "impact": "none", "signature": _signature_data_url()}

    login(client, tenant_a.viewer)
    assert client.post(reverse("work_order_complete", args=[wo.pk]), data).status_code == 403

    login(client, tenant_a.tech)
    client.post(reverse("checklist_answer", args=[wo.pk, item.pk]), {"result": "ok"})
    assert client.post(reverse("work_order_complete", args=[wo.pk]), data).status_code == 302
    with tenant_context(tenant_a.org):
        assert WorkOrder.objects.get(pk=wo.pk).status == "closed"
        signature = Attachment.objects.get(target_id=wo.pk, purpose=Purpose.SIGNATURE)
        assert signature.uploaded_by == tenant_a.tech.user
