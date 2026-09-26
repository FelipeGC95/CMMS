"""Aislamiento del alta y edición de activos (AST-02/03): la organización A no ve,
edita ni referencia activos, clases o ubicaciones de B."""

import pytest
from django.core.exceptions import ValidationError
from django.urls import reverse

from apps.assets.models import Asset
from apps.assets.services import create_asset, update_asset
from apps.core.tenancy import tenant_context
from apps.organizations.models import Location
from tests.conftest import login


@pytest.fixture
def foreign_location(tenant_b):
    with tenant_context(tenant_b.org):
        return Location.objects.create(code="B-SITE", name="Planta B")


def test_edit_view_of_foreign_asset_is_404(client, tenant_a, tenant_b):
    login(client, tenant_b.admin)
    url = reverse("asset_edit", args=[tenant_a.asset.pk])
    assert client.get(url).status_code == 404
    assert client.post(url, {"code": "HACK", "name": "x"}).status_code == 404
    tenant_a.asset.refresh_from_db()
    assert tenant_a.asset.code == "CMP-01"


def test_create_view_does_not_accept_foreign_references(client, tenant_a, tenant_b, foreign_location):
    login(client, tenant_a.admin)
    response = client.post(
        reverse("asset_create"),
        {
            "code": "NEW-1",
            "name": "Nuevo",
            "asset_class": tenant_b.asset.asset_class_id,
            "location": foreign_location.pk,
            "parent": tenant_b.asset.pk,
            "criticality": "B",
        },
    )
    assert response.status_code == 200
    errors = response.context["form"].errors
    assert {"asset_class", "location", "parent"} <= set(errors)
    with tenant_context(tenant_a.org):
        assert not Asset.objects.filter(code="NEW-1").exists()


def test_create_form_only_offers_own_organization_choices(client, tenant_a, tenant_b, foreign_location):
    login(client, tenant_a.admin)
    form = client.get(reverse("asset_create")).context["form"]
    assert tenant_b.asset.asset_class not in form.fields["asset_class"].queryset
    assert foreign_location not in form.fields["location"].queryset
    assert tenant_b.asset not in form.fields["parent"].queryset


def test_create_view_ignores_foreign_parent_prefill(client, tenant_a, tenant_b):
    login(client, tenant_a.admin)
    response = client.get(reverse("asset_create"), {"parent": tenant_b.asset.pk})
    assert "parent" not in response.context["form"].initial


def test_attribute_fragment_ignores_foreign_class_and_asset(client, tenant_a, tenant_b):
    from apps.assets.services import add_class_attribute

    with tenant_context(tenant_b.org):
        add_class_attribute(
            membership=tenant_b.admin,
            asset_class=tenant_b.asset.asset_class,
            label="Secreto",
            data_type="text",
        )
    login(client, tenant_a.admin)
    response = client.get(
        reverse("asset_attribute_fields"),
        {"asset_class": tenant_b.asset.asset_class_id, "asset": tenant_b.asset.pk},
    )
    assert response.status_code == 200
    assert "Secreto" not in response.content.decode()


def test_asset_list_by_foreign_parent_is_404(client, tenant_a, tenant_b):
    login(client, tenant_a.admin)
    assert client.get(reverse("asset_list"), {"parent": tenant_b.asset.pk}).status_code == 404


def test_services_reject_foreign_references(tenant_a, tenant_b, foreign_location):
    with tenant_context(tenant_a.org):
        for field, value in [
            ("asset_class", tenant_b.asset.asset_class),
            ("location", foreign_location),
            ("parent", tenant_b.asset),
        ]:
            values = {"code": "X-1", "name": "x", "asset_class": tenant_a.asset.asset_class, field: value}
            with pytest.raises(ValidationError) as error:
                create_asset(membership=tenant_a.admin, **values)
            assert field in error.value.message_dict
        with pytest.raises(ValidationError):
            update_asset(
                membership=tenant_a.admin,
                asset=tenant_a.asset,
                code="CMP-01",
                name="Compresor 1",
                asset_class=tenant_a.asset.asset_class,
                parent=tenant_b.asset,
            )


def test_same_code_is_allowed_in_another_organization(tenant_a, tenant_b):
    with tenant_context(tenant_b.org):
        asset = create_asset(
            membership=tenant_b.admin, code="NEW-1", name="B", asset_class=tenant_b.asset.asset_class
        )
    with tenant_context(tenant_a.org):
        other = create_asset(
            membership=tenant_a.admin, code="NEW-1", name="A", asset_class=tenant_a.asset.asset_class
        )
    assert asset.organization_id != other.organization_id
