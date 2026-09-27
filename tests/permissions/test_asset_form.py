"""Permisos del alta y edición de activos (AST-02): solo roles con `asset.manage`,
y con alcance por sitio (ORG-06) solo dentro de sus ubicaciones."""

import pytest
from django.core.exceptions import ValidationError
from django.urls import reverse

from apps.accounts.models import MembershipLocation
from apps.accounts.permissions import PermissionDeniedError
from apps.assets.models import Asset
from apps.assets.services import create_asset, update_asset
from apps.core.tenancy import tenant_context
from apps.organizations.models import Location
from tests.conftest import login


@pytest.mark.parametrize("role", ["tech", "viewer"])
def test_roles_without_asset_manage_get_403(client, tenant_a, role):
    login(client, getattr(tenant_a, role))
    for url in [
        reverse("asset_create"),
        reverse("asset_edit", args=[tenant_a.asset.pk]),
        reverse("asset_attribute_fields"),
    ]:
        assert client.get(url).status_code == 403, url
    data = {"code": "X", "name": "x", "asset_class": tenant_a.asset.asset_class_id, "criticality": "B"}
    assert client.post(reverse("asset_create"), data).status_code == 403
    with tenant_context(tenant_a.org):
        assert not Asset.objects.filter(code="X").exists()


@pytest.mark.parametrize("role", ["tech", "viewer"])
def test_services_require_asset_manage(tenant_a, role):
    membership = getattr(tenant_a, role)
    with tenant_context(tenant_a.org):
        with pytest.raises(PermissionDeniedError):
            create_asset(membership=membership, code="X", name="x", asset_class=tenant_a.asset.asset_class)
        with pytest.raises(PermissionDeniedError):
            update_asset(
                membership=membership,
                asset=tenant_a.asset,
                code="CMP-01",
                name="hack",
                asset_class=tenant_a.asset.asset_class,
            )


def test_buttons_only_for_managers(client, tenant_a):
    login(client, tenant_a.tech)
    assert reverse("asset_create") not in client.get(reverse("asset_list")).content.decode()
    assert (
        reverse("asset_edit", args=[tenant_a.asset.pk])
        not in client.get(reverse("asset_detail", args=[tenant_a.asset.pk])).content.decode()
    )
    login(client, tenant_a.admin)
    assert reverse("asset_create") in client.get(reverse("asset_list")).content.decode()
    assert reverse("asset_create") in client.get(reverse("menu")).content.decode()


@pytest.fixture
def scoped_planner(tenant_a):
    """Planificador limitado a Planta Norte; Planta Sur queda fuera."""
    with tenant_context(tenant_a.org):
        north = Location.objects.create(code="N", name="Planta Norte")
        south = Location.objects.create(code="S", name="Planta Sur")
        cls = tenant_a.asset.asset_class
        inside = Asset.objects.create(code="IN-01", name="Llenadora", asset_class=cls, location=north)
        outside = Asset.objects.create(code="OUT-01", name="Caldera", asset_class=cls, location=south)
    membership = tenant_a.admin
    membership.role = "planner"
    membership.restricted_to_locations = True
    membership.save()
    with tenant_context(tenant_a.org):
        MembershipLocation.objects.create(membership=membership, location=north)
    return membership, north, south, inside, outside


def test_scoped_member_must_create_inside_its_sites(tenant_a, scoped_planner):
    membership, north, south, inside, outside = scoped_planner
    cls = tenant_a.asset.asset_class
    with tenant_context(tenant_a.org):
        for location in (None, south):
            with pytest.raises(ValidationError) as error:
                create_asset(membership=membership, code="S-1", name="s", asset_class=cls, location=location)
            assert "location" in error.value.message_dict
        with pytest.raises(ValidationError) as error:
            create_asset(
                membership=membership, code="S-1", name="s", asset_class=cls, location=north, parent=outside
            )
        assert "parent" in error.value.message_dict
        asset = create_asset(
            membership=membership, code="S-1", name="s", asset_class=cls, location=north, parent=inside
        )
    assert asset.location == north


def test_scoped_member_cannot_edit_or_move_assets_outside_its_sites(client, tenant_a, scoped_planner):
    membership, north, south, inside, outside = scoped_planner
    login(client, membership)
    assert client.get(reverse("asset_edit", args=[outside.pk])).status_code == 404
    form = client.get(reverse("asset_edit", args=[inside.pk])).context["form"]
    with tenant_context(membership.organization):  # queryset perezoso: se evalúa aquí, con RLS
        assert list(form.fields["location"].queryset) == [north]
    assert outside not in form.fields["parent"].queryset
    with tenant_context(tenant_a.org):
        with pytest.raises(PermissionDeniedError):
            update_asset(
                membership=membership,
                asset=outside,
                code="OUT-01",
                name="x",
                asset_class=outside.asset_class,
                location=south,
            )
        with pytest.raises(ValidationError):
            update_asset(
                membership=membership,
                asset=inside,
                code="IN-01",
                name="x",
                asset_class=inside.asset_class,
                location=south,
            )
