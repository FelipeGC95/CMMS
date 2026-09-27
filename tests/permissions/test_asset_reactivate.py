"""Permisos de la reactivación (AST-04): solo roles con `asset.manage` y dentro de su alcance."""

import pytest
from django.urls import reverse

from apps.accounts.models import MembershipLocation
from apps.accounts.permissions import PermissionDeniedError
from apps.assets.models import Asset
from apps.assets.services import change_asset_status, reactivate_asset
from apps.core.tenancy import platform_context, tenant_context
from apps.organizations.models import Location
from tests.conftest import login


@pytest.fixture
def retired_a(tenant_a):
    with tenant_context(tenant_a.org):
        change_asset_status(asset=tenant_a.asset, status="retired", reason="Baja")
    return tenant_a.asset


@pytest.mark.parametrize("role", ["tech", "viewer"])
def test_view_forbidden_without_asset_manage(client, tenant_a, retired_a, role):
    login(client, getattr(tenant_a, role))
    url = reverse("asset_reactivate", args=[retired_a.pk])
    assert client.get(url).status_code == 403
    assert client.post(url, {"reason": "x"}).status_code == 403
    html = client.get(reverse("asset_detail", args=[retired_a.pk])).content.decode()
    assert url not in html
    # Fuera del contexto de la petición la fila solo se ve con bypass de RLS (ADR-026).
    with platform_context():
        retired_a.refresh_from_db()
    assert retired_a.status == "retired"


@pytest.mark.parametrize("role", ["tech", "viewer"])
def test_service_requires_asset_manage(tenant_a, retired_a, role):
    with tenant_context(tenant_a.org):
        with pytest.raises(PermissionDeniedError):
            reactivate_asset(membership=getattr(tenant_a, role), asset=retired_a, reason="x")


def test_scoped_manager_only_reactivates_in_its_sites(client, tenant_a):
    with tenant_context(tenant_a.org):
        north = Location.objects.create(code="N", name="Planta Norte")
        south = Location.objects.create(code="S", name="Planta Sur")
        cls = tenant_a.asset.asset_class
        inside = Asset.objects.create(code="IN-01", name="Llenadora", asset_class=cls, location=north)
        outside = Asset.objects.create(code="OUT-01", name="Caldera", asset_class=cls, location=south)
        for asset in (inside, outside):
            change_asset_status(asset=asset, status="retired", reason="Baja")
    membership = tenant_a.admin
    membership.role = "planner"
    membership.restricted_to_locations = True
    membership.save()
    with tenant_context(tenant_a.org):
        MembershipLocation.objects.create(membership=membership, location=north)

    login(client, membership)
    assert client.get(reverse("asset_reactivate", args=[outside.pk])).status_code == 404
    assert client.get(reverse("asset_reactivate", args=[inside.pk])).status_code == 200
    with tenant_context(tenant_a.org):
        with pytest.raises(PermissionDeniedError):
            reactivate_asset(membership=membership, asset=outside, reason="x")
        reactivate_asset(membership=membership, asset=inside, reason="ok")
        inside.refresh_from_db()
        assert inside.status == "operational"
