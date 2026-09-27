"""KPI-01 / KPI-03: la organización B no ve ni exporta datos de A, ni por ids ni por filtros."""

from django.urls import reverse

from apps.core.tenancy import tenant_context
from apps.organizations.models import Location
from apps.work.services import create_work_order
from tests.conftest import login
from tests.test_exports_excel import xlsx_rows


def test_excel_exports_only_contain_own_organization(client, tenant_a, tenant_b):
    with tenant_context(tenant_a.org):
        create_work_order(work_type="corrective", title="Solo de A", asset=tenant_a.asset)
    login(client, tenant_b.admin)
    assets = xlsx_rows(client.get(reverse("asset_export_xlsx")))
    assert [r[0] for r in assets[1:]] == ["CMP-01"]  # el de B (mismo código, otra organización)
    orders = xlsx_rows(client.get(reverse("work_order_export_xlsx")))
    assert orders[1:] == []


def test_foreign_parent_filter_is_404(client, tenant_a, tenant_b):
    login(client, tenant_b.admin)
    response = client.get(reverse("asset_export_xlsx"), {"parent": tenant_a.asset.pk})
    assert response.status_code == 404


def test_foreign_location_filter_is_ignored(client, tenant_a, tenant_b):
    with tenant_context(tenant_a.org):
        site = Location.objects.create(code="A-S", name="Sitio A")
        create_work_order(work_type="corrective", title="En sitio A", location=site)
    login(client, tenant_b.admin)
    orders = xlsx_rows(client.get(reverse("work_order_export_xlsx"), {"location": site.pk}))
    assert orders[1:] == []
    response = client.get(reverse("dashboard"), {"location": site.pk})
    assert "location" in response.context["filter_form"].errors


def test_dashboard_filters_reject_foreign_class_and_asset(client, tenant_a, tenant_b):
    with tenant_context(tenant_a.org):
        from apps.assets.models import Asset

        Asset.objects.create(code="SOLO-A", name="x", asset_class=tenant_a.asset.asset_class)
    login(client, tenant_b.admin)
    response = client.get(
        reverse("dashboard"), {"asset_class": tenant_a.asset.asset_class_id, "asset": "SOLO-A"}
    )
    errors = response.context["filter_form"].errors
    assert "asset_class" in errors and "asset" in errors
