"""Completar con firma cuando el cupo de almacenamiento está lleno (WO-03 CA3, AST-06 CA2)."""

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from apps.attachments.models import Attachment, Purpose
from apps.core.tenancy import tenant_context
from apps.work.models import WorkOrder
from apps.work.services import (
    WorkOrderError,
    add_checklist_item,
    complete_work_order,
    report_failure,
)
from tests.conftest import login
from tests.test_wo_checklist import _fill_quota, _signature_data_url, _signature_png


@pytest.fixture(autouse=True)
def _media(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path


def _wo_with_items(tenant, *texts):
    wo = report_failure(
        membership=tenant.viewer, asset=tenant.asset, description="Falla", asset_stopped=False
    ).work_order
    items = [add_checklist_item(work_order=wo, membership=tenant.admin, text=t) for t in texts]
    return wo, items


# --- Firma con el cupo lleno -------------------------------------------------------------


def test_completion_with_signature_and_full_quota_skips_the_signature(tenant_a):
    _fill_quota(tenant_a)
    with tenant_context(tenant_a.org):
        wo, _items = _wo_with_items(tenant_a)
        signature = SimpleUploadedFile("firma.png", _signature_png(), content_type="image/png")

        done = complete_work_order(
            work_order=wo, membership=tenant_a.tech, labor_minutes=10, signature=signature
        )

        assert done.status in ("completed", "closed")
        assert "sin la firma" in done.signature_warning
        assert not Attachment.objects.filter(target_id=wo.pk, purpose=Purpose.SIGNATURE).exists()


def test_completion_with_an_invalid_signature_image_still_fails(in_a):
    wo, _items = _wo_with_items(in_a)
    bad = SimpleUploadedFile("firma.png", b"basura", content_type="image/png")
    with pytest.raises(WorkOrderError, match="No se pudo leer"):
        complete_work_order(work_order=wo, membership=in_a.tech, labor_minutes=10, signature=bad)


def test_complete_view_warns_when_signature_did_not_fit(client, tenant_a):
    _fill_quota(tenant_a)
    with tenant_context(tenant_a.org):
        wo, _items = _wo_with_items(tenant_a)
    login(client, tenant_a.tech)
    response = client.post(
        reverse("work_order_complete", args=[wo.pk]),
        {"labor_minutes": 20, "impact": "none", "signature": _signature_data_url()},
        follow=True,
    )
    html = response.content.decode()
    assert "completada" in html
    assert "alert--warning" in html and "sin la firma" in html
    with tenant_context(tenant_a.org):
        assert WorkOrder.objects.get(pk=wo.pk).status in ("completed", "closed")
