"""Correctiva desde un hallazgo No OK (WO-06 CA1)."""

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from apps.core.tenancy import tenant_context
from apps.work.models import WorkOrder, WorkRequest
from apps.work.services import (
    WorkOrderError,
    add_checklist_photo,
    answer_checklist_item,
    create_corrective_from_item,
    create_work_order,
)
from tests.conftest import login, tiny_jpeg_bytes


@pytest.fixture(autouse=True)
def _media(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path


def _finding(tenant):
    wo = create_work_order(
        work_type="preventive", title="Mensual", asset=tenant.asset, checklist=list(tenant.plan.items.all())
    )
    check, measure = wo.checklist.all()
    answer_checklist_item(item=check, membership=tenant.tech, result="not_ok", note="Fuga en válvula")
    answer_checklist_item(item=measure, membership=tenant.tech, value_number="9,1")  # fuera de 6,5–8
    photo = SimpleUploadedFile("f.jpg", tiny_jpeg_bytes(), content_type="image/jpeg")
    add_checklist_photo(item=check, membership=tenant.tech, uploaded_file=photo)
    return wo, check, measure


def test_manager_creates_a_linked_corrective_directly(in_a):
    wo, check, _measure = _finding(in_a)
    corrective = create_corrective_from_item(item=check, membership=in_a.admin)
    assert (corrective.work_type, corrective.status, corrective.asset_id) == (
        "corrective",
        "planned",
        in_a.asset.pk,
    )
    assert (corrective.parent_work_order_id, corrective.source_checklist_item_id) == (wo.pk, check.pk)
    assert "Fuga en válvula" in corrective.description and corrective.title == "Purgar"
    assert not WorkRequest.objects.filter(work_order=corrective).exists()
    # Idempotente mientras siga abierta.
    assert create_corrective_from_item(item=check, membership=in_a.admin).pk == corrective.pk


def test_technician_reports_the_failure_prefilled(in_a):
    wo, _check, measure = _finding(in_a)
    corrective = create_corrective_from_item(item=measure, membership=in_a.tech, asset_stopped=True)
    request = WorkRequest.objects.get(work_order=corrective)
    assert corrective.status == "requested" and request.asset_stopped
    assert corrective.parent_work_order_id == wo.pk and "9,1" not in corrective.title
    assert "(9,1 bar)" in corrective.description


def test_only_not_ok_check_or_measurement_items(in_a):
    wo = create_work_order(
        work_type="preventive", title="x", asset=in_a.asset, checklist=list(in_a.plan.items.all())
    )
    check = wo.checklist.first()
    answer_checklist_item(item=check, membership=in_a.tech, result="ok")
    with pytest.raises(WorkOrderError):
        create_corrective_from_item(item=check, membership=in_a.admin)


def test_screens_show_the_link_on_both_work_orders(client, tenant_a):
    with tenant_context(tenant_a.org):
        wo, check, _measure = _finding(tenant_a)
    login(client, tenant_a.admin)
    url = reverse("checklist_item_corrective", args=[wo.pk, check.pk])
    detail = client.get(reverse("work_order_detail", args=[wo.pk])).content.decode()
    assert url in detail
    page = client.get(url).content.decode()
    assert "Fotos del hallazgo" in page
    response = client.post(url, {"description": "Cambiar sello"})
    with tenant_context(tenant_a.org):
        corrective = WorkOrder.objects.get(source_checklist_item=check)
    assert response.url == reverse("work_order_detail", args=[corrective.pk])
    child = client.get(response.url).content.decode()
    assert wo.code in child and "Foto 1 del hallazgo" in child
    assert corrective.code in client.get(reverse("work_order_detail", args=[wo.pk])).content.decode()
