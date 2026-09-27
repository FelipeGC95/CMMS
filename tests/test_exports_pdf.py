"""KPI-03: ficha de activo y OT en PDF con la marca de la organización (texto leído con pypdf)."""

import io
from decimal import Decimal

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from pypdf import PdfReader

from apps.assets.models import Meter
from apps.core.tenancy import tenant_context
from apps.maintenance.services import assign_plan
from apps.work.services import (
    add_other_cost,
    add_part_usage,
    answer_checklist_item,
    complete_work_order,
    create_work_order,
    report_failure,
    transition,
)
from tests.conftest import login
from tests.test_wo_checklist import _signature_png


def pdf_text(response):
    assert response.status_code == 200
    assert response["Content-Type"] == "application/pdf"
    reader = PdfReader(io.BytesIO(response.content))
    return reader, "\n".join(page.extract_text() for page in reader.pages)


@pytest.fixture
def asset_data(tenant_a):
    with tenant_context(tenant_a.org):
        asset = tenant_a.asset
        asset.manufacturer = "Atlas Ñandú"
        asset.save()
        Meter.objects.create(
            asset=asset, kind="hours", unit="h", name="Horómetro", current_value=Decimal("1250")
        )
        assign_plan(plan=tenant_a.plan, asset=asset, by=tenant_a.admin.user)
        report_failure(membership=tenant_a.viewer, asset=asset, description="Pierde aire", asset_stopped=True)
    return asset


def test_asset_sheet_pdf_has_data_meters_plans_work_orders_and_failures(client, tenant_a, asset_data):
    login(client, tenant_a.admin)
    _reader, text = pdf_text(client.get(reverse("asset_pdf", args=[asset_data.pk])))
    for expected in (
        "Ficha de activo CMP-01",
        "Compresor 1",
        "Atlas Ñandú",
        "Horómetro",
        "1.250",
        "Mensual",
        "Detención total",
        "Empresa-A",
    ):
        assert expected in text, expected


def _completed_work_order(tenant):
    wo = create_work_order(
        work_type="preventive",
        title="PM compresor",
        asset=tenant.asset,
        checklist=tenant.plan.items.all(),
        assigned_to=tenant.tech.user,
    )
    transition(work_order=wo, to_status="in_progress", membership=tenant.tech)
    check, measure = wo.checklist.order_by("order")
    answer_checklist_item(item=check, membership=tenant.tech, result="ok", note="Sin agua")
    answer_checklist_item(item=measure, membership=tenant.tech, value_number=Decimal("7.2"))
    add_part_usage(
        work_order=wo,
        membership=tenant.tech,
        description="Filtro",
        quantity=Decimal("2"),
        unit_cost=Decimal("15000"),
    )
    add_other_cost(work_order=wo, membership=tenant.admin, description="Grúa", amount=Decimal("50000"))
    signature = SimpleUploadedFile("firma.png", _signature_png(), content_type="image/png")
    complete_work_order(work_order=wo, membership=tenant.tech, labor_minutes=30, signature=signature)
    wo.refresh_from_db()
    return wo


def test_work_order_pdf_has_header_checklist_costs_and_signature(client, tenant_a):
    with tenant_context(tenant_a.org):
        wo = _completed_work_order(tenant_a)
    login(client, tenant_a.admin)
    reader, text = pdf_text(client.get(reverse("work_order_pdf", args=[wo.pk])))
    for expected in (
        f"Orden de trabajo {wo.code}",
        "PM compresor",
        "Preventiva",
        "Purgar",
        "Sin agua",
        "7,2 bar",
        "Filtro",
        "Grúa",
        "Costo total",
        "Firma de conformidad",
    ):
        assert expected in text, expected
    assert any(page.images for page in reader.pages)  # la firma va como imagen


def test_work_order_pdf_without_checklist_or_costs(client, tenant_a):
    with tenant_context(tenant_a.org):
        wo = create_work_order(work_type="corrective", title="Revisar ruido", asset=tenant_a.asset)
    login(client, tenant_a.viewer)
    _reader, text = pdf_text(client.get(reverse("work_order_pdf", args=[wo.pk])))
    assert "Sin checklist." in text and "Sin costos." in text


def test_detail_pages_link_to_their_pdf(client, tenant_a):
    with tenant_context(tenant_a.org):
        wo = create_work_order(work_type="corrective", title="x", asset=tenant_a.asset)
    login(client, tenant_a.admin)
    assert (
        reverse("asset_pdf", args=[tenant_a.asset.pk])
        in client.get(reverse("asset_detail", args=[tenant_a.asset.pk])).content.decode()
    )
    assert (
        reverse("work_order_pdf", args=[wo.pk])
        in client.get(reverse("work_order_detail", args=[wo.pk])).content.decode()
    )


def test_pdf_query_budget(client, tenant_a, asset_data, django_assert_max_num_queries):
    with tenant_context(tenant_a.org):
        wo = _completed_work_order(tenant_a)
    login(client, tenant_a.admin)
    with django_assert_max_num_queries(20):
        assert client.get(reverse("asset_pdf", args=[asset_data.pk])).status_code == 200
    with django_assert_max_num_queries(20):
        assert client.get(reverse("work_order_pdf", args=[wo.pk])).status_code == 200
