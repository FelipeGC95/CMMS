"""Aislamiento de la reactivación (AST-04): B no reactiva activos de A ni sus planes, ni ve
los componentes de A en la confirmación de baja."""

import pytest
from django.core.exceptions import ValidationError
from django.urls import reverse

from apps.accounts.permissions import PermissionDeniedError
from apps.assets.models import Asset
from apps.assets.services import decommission_asset, reactivate_asset
from apps.core.tenancy import tenant_context
from apps.maintenance.models import PlanAssignment
from apps.maintenance.services import assign_plan
from tests.conftest import login


@pytest.fixture
def retired_a(tenant_a):
    with tenant_context(tenant_a.org):
        assignment = assign_plan(plan=tenant_a.plan, asset=tenant_a.asset)
        decommission_asset(membership=tenant_a.admin, asset=tenant_a.asset, reason="Baja")
    return tenant_a.asset, assignment


def test_foreign_reactivate_view_is_404(client, tenant_a, tenant_b, retired_a):
    asset, assignment = retired_a
    login(client, tenant_b.admin)
    url = reverse("asset_reactivate", args=[asset.pk])
    assert client.get(url).status_code == 404
    assert client.post(url, {"reason": "hack", "assignments": [str(assignment.pk)]}).status_code == 404
    with tenant_context(tenant_a.org):
        asset.refresh_from_db()
        assert asset.status == "retired"


def test_service_rejects_foreign_asset_and_assignments(tenant_a, tenant_b, retired_a):
    asset, assignment = retired_a
    with tenant_context(tenant_b.org):
        with pytest.raises(PermissionDeniedError):
            reactivate_asset(
                membership=tenant_b.admin, asset=asset, reason="x", assignment_ids=[assignment.pk]
            )
    # Un activo propio de B tampoco puede reactivar una asignación de A.
    with tenant_context(tenant_b.org):
        decommission_asset(membership=tenant_b.admin, asset=tenant_b.asset, reason="Baja")
        with pytest.raises(ValidationError):  # no es una asignación de este activo
            reactivate_asset(
                membership=tenant_b.admin, asset=tenant_b.asset, reason="x", assignment_ids=[assignment.pk]
            )
    with tenant_context(tenant_a.org):
        assert PlanAssignment.objects.get(pk=assignment.pk).is_active is False


def test_decommission_page_does_not_list_foreign_components(client, tenant_a, tenant_b):
    with tenant_context(tenant_a.org):
        Asset.objects.create(
            code="A-CHILD", name="Motor A", asset_class=tenant_a.asset.asset_class, parent=tenant_a.asset
        )
    login(client, tenant_b.admin)
    response = client.get(reverse("asset_decommission", args=[tenant_b.asset.pk]))
    assert list(response.context["active_children"]) == []
    assert "A-CHILD" not in response.content.decode()
