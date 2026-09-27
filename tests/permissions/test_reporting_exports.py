"""KPI-01 / KPI-03: exportaciones y filtros con los mismos permisos y alcance por sitio que su página."""

import pytest
from django.urls import reverse

from apps.accounts.models import Membership, MembershipLocation, User
from apps.assets.models import Asset
from apps.core.tenancy import tenant_context
from apps.organizations.models import Location
from apps.work.services import create_work_order
from tests.conftest import PASSWORD, login
from tests.test_exports_excel import xlsx_rows


@pytest.fixture
def sites(tenant_a):
    """Planta Norte (con su área) y Planta Sur, con un activo y una OT en cada una."""
    with tenant_context(tenant_a.org):
        north = Location.objects.create(code="N", name="Planta Norte")
        area = Location.objects.create(code="N-A", name="Envasado", kind="area", parent=north)
        south = Location.objects.create(code="S", name="Planta Sur")
        cls = tenant_a.asset.asset_class
        inside = Asset.objects.create(code="IN-01", name="Llenadora", asset_class=cls, location=area)
        outside = Asset.objects.create(code="OUT-01", name="Caldera", asset_class=cls, location=south)
        create_work_order(work_type="corrective", title="Dentro", asset=inside)
        create_work_order(work_type="corrective", title="Fuera", asset=outside)
    return {"north": north, "south": south, "inside": inside, "outside": outside}


def _member(tenant, role, location):
    user = User.objects.create_user(f"{role}-scoped@{tenant.org.slug}.cl", PASSWORD, full_name=role)
    member = Membership.objects.create(
        user=user, organization=tenant.org, role=role, restricted_to_locations=True
    )
    with tenant_context(tenant.org):
        MembershipLocation.objects.create(membership=member, location=location)
    return member


@pytest.mark.parametrize("role", ["external", "technician"])
def test_scoped_exports_only_contain_their_sites(client, tenant_a, sites, role):
    login(client, _member(tenant_a, role, sites["north"]))
    assets = xlsx_rows(client.get(reverse("asset_export_xlsx")))
    assert [r[0] for r in assets[1:]] == ["IN-01"]
    orders = xlsx_rows(client.get(reverse("work_order_export_xlsx"), {"scope": "open"}))
    assert [r[4] for r in orders[1:]] == ["Dentro"]


def test_scoped_member_cannot_filter_outside_their_sites(client, tenant_a, sites):
    login(client, _member(tenant_a, "external", sites["north"]))
    orders = xlsx_rows(client.get(reverse("work_order_export_xlsx"), {"location": sites["south"].pk}))
    assert [r[4] for r in orders[1:]] == ["Dentro"]  # filtro ajeno al alcance: se ignora
    response = client.get(reverse("dashboard"), {"location": sites["south"].pk, "asset": "OUT-01"})
    errors = response.context["filter_form"].errors
    assert "location" in errors and "asset" in errors


def test_viewer_can_export_like_they_can_read(client, tenant_a, sites):
    login(client, tenant_a.viewer)
    assert client.get(reverse("asset_export_xlsx")).status_code == 200
    assert client.get(reverse("work_order_export_xlsx")).status_code == 200


def test_exports_require_login(client, tenant_a):
    for name in ("asset_export_xlsx", "work_order_export_xlsx"):
        response = client.get(reverse(name))
        assert response.status_code == 302 and "login" in response["Location"]


@pytest.mark.parametrize("role", ["external", "technician"])
def test_scoped_pdfs_follow_the_detail_scope(client, tenant_a, sites, role):
    with tenant_context(tenant_a.org):
        from apps.work.models import WorkOrder

        inside_wo = WorkOrder.objects.get(title="Dentro")
        outside_wo = WorkOrder.objects.get(title="Fuera")
    login(client, _member(tenant_a, role, sites["north"]))
    assert client.get(reverse("asset_pdf", args=[sites["inside"].pk])).status_code == 200
    assert client.get(reverse("work_order_pdf", args=[inside_wo.pk])).status_code == 200
    assert client.get(reverse("asset_pdf", args=[sites["outside"].pk])).status_code == 404
    assert client.get(reverse("work_order_pdf", args=[outside_wo.pk])).status_code == 404


def test_scoped_asset_pdf_shows_the_asset_work_orders(client, tenant_a, sites):
    from tests.test_exports_pdf import pdf_text

    login(client, _member(tenant_a, "external", sites["north"]))
    _reader, text = pdf_text(client.get(reverse("asset_pdf", args=[sites["inside"].pk])))
    assert "Dentro" in text and "Fuera" not in text
