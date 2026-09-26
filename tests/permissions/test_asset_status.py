"""Permisos del cambio de estado y la baja (AST-04): solo roles con `asset.manage` y,
con alcance por sitio (ORG-06), solo en sus ubicaciones; lo mismo para los componentes."""

import pytest
from django.urls import reverse

from apps.accounts.models import MembershipLocation
from apps.accounts.permissions import PermissionDeniedError
from apps.assets.models import Asset
from apps.assets.selectors import asset_scope_ids
from apps.assets.services import decommission_asset, set_asset_status
from apps.core.tenancy import tenant_context
from apps.organizations.models import Location
from tests.conftest import login


@pytest.mark.parametrize("role", ["tech", "viewer"])
def test_views_forbidden_without_asset_manage(client, tenant_a, role):
    login(client, getattr(tenant_a, role))
    for name in ("asset_status", "asset_decommission"):
        url = reverse(name, args=[tenant_a.asset.pk])
        assert client.get(url).status_code == 403
        assert client.post(url, {"status": "stopped", "reason": "x"}).status_code == 403
    tenant_a.asset.refresh_from_db()
    assert tenant_a.asset.status == "operational"


@pytest.mark.parametrize("role", ["tech", "viewer"])
def test_services_require_asset_manage(tenant_a, role):
    membership = getattr(tenant_a, role)
    with tenant_context(tenant_a.org):
        with pytest.raises(PermissionDeniedError):
            set_asset_status(membership=membership, asset=tenant_a.asset, status="stopped", reason="x")
        with pytest.raises(PermissionDeniedError):
            decommission_asset(membership=membership, asset=tenant_a.asset, reason="x")


def test_buttons_only_for_managers(client, tenant_a):
    status_url = reverse("asset_status", args=[tenant_a.asset.pk])
    decommission_url = reverse("asset_decommission", args=[tenant_a.asset.pk])
    detail_url = reverse("asset_detail", args=[tenant_a.asset.pk])
    login(client, tenant_a.tech)
    html = client.get(detail_url).content.decode()
    assert status_url not in html and decommission_url not in html
    login(client, tenant_a.admin)
    html = client.get(detail_url).content.decode()
    assert status_url in html and decommission_url in html


@pytest.fixture
def scoped_planner(tenant_a):
    """Planificador limitado a Planta Norte; un componente de su activo está en Planta Sur."""
    with tenant_context(tenant_a.org):
        north = Location.objects.create(code="N", name="Planta Norte")
        south = Location.objects.create(code="S", name="Planta Sur")
        cls = tenant_a.asset.asset_class
        inside = Asset.objects.create(code="IN-01", name="Llenadora", asset_class=cls, location=north)
        inside_child = Asset.objects.create(
            code="IN-02", name="Motor", asset_class=cls, location=north, parent=inside
        )
        outside_child = Asset.objects.create(
            code="OUT-02", name="Bomba", asset_class=cls, location=south, parent=inside
        )
        outside = Asset.objects.create(code="OUT-01", name="Caldera", asset_class=cls, location=south)
    membership = tenant_a.admin
    membership.role = "planner"
    membership.restricted_to_locations = True
    membership.save()
    with tenant_context(tenant_a.org):
        MembershipLocation.objects.create(membership=membership, location=north)
    return membership, inside, inside_child, outside_child, outside


def test_scoped_member_only_changes_assets_in_its_sites(client, tenant_a, scoped_planner):
    membership, inside, _inside_child, _outside_child, outside = scoped_planner
    login(client, membership)
    assert client.get(reverse("asset_status", args=[outside.pk])).status_code == 404
    assert client.get(reverse("asset_decommission", args=[outside.pk])).status_code == 404
    with tenant_context(tenant_a.org):
        with pytest.raises(PermissionDeniedError):
            set_asset_status(membership=membership, asset=outside, status="stopped", reason="x")
        with pytest.raises(PermissionDeniedError):
            decommission_asset(membership=membership, asset=outside, reason="x")
        set_asset_status(membership=membership, asset=inside, status="stopped", reason="ok")


def test_components_outside_scope_are_not_included(tenant_a, scoped_planner):
    membership, inside, inside_child, outside_child, _outside = scoped_planner
    with tenant_context(tenant_a.org):
        ids = asset_scope_ids(membership=membership, asset=inside, include_components=True)
    assert set(ids) == {inside.pk, inside_child.pk}
