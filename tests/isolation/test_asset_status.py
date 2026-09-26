"""Aislamiento de estados, baja y "incluir componentes" (AST-03/04): la organización B no
cambia el estado de activos de A ni ve sus componentes en historial o KPI."""

import datetime

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.accounts.permissions import PermissionDeniedError
from apps.assets.models import Asset, AssetStatusPeriod
from apps.assets.selectors import asset_scope_ids
from apps.assets.services import decommission_asset, set_asset_status
from apps.core.tenancy import tenant_context
from apps.maintenance.services import assign_plan
from apps.reporting.selectors import asset_kpis
from apps.work.models import FailureEvent
from tests.conftest import login


@pytest.fixture
def child_of_a(tenant_a):
    with tenant_context(tenant_a.org):
        child = Asset.objects.create(
            code="A-CHILD", name="Motor A", asset_class=tenant_a.asset.asset_class, parent=tenant_a.asset
        )
        start = timezone.now() - datetime.timedelta(days=2)
        AssetStatusPeriod.objects.create(
            asset=child, status="stopped", started_at=start, ended_at=start + datetime.timedelta(hours=5)
        )
        FailureEvent.objects.create(
            asset=child, impact="total_stop", started_at=start, ended_at=start + datetime.timedelta(hours=5)
        )
    return child


def test_foreign_status_and_decommission_views_are_404(client, tenant_a, tenant_b):
    login(client, tenant_b.admin)
    for name in ("asset_status", "asset_decommission"):
        url = reverse(name, args=[tenant_a.asset.pk])
        assert client.get(url).status_code == 404
        assert client.post(url, {"status": "stopped", "reason": "hack"}).status_code == 404
    tenant_a.asset.refresh_from_db()
    assert tenant_a.asset.status == "operational"


def test_services_reject_foreign_asset(tenant_a, tenant_b):
    with tenant_context(tenant_a.org):
        assign_plan(plan=tenant_a.plan, asset=tenant_a.asset)
    with tenant_context(tenant_b.org):
        with pytest.raises(PermissionDeniedError):
            set_asset_status(membership=tenant_b.admin, asset=tenant_a.asset, status="stopped", reason="x")
        with pytest.raises(PermissionDeniedError):
            decommission_asset(membership=tenant_b.admin, asset=tenant_a.asset, reason="x")
    with tenant_context(tenant_a.org):
        tenant_a.asset.refresh_from_db()
        assert tenant_a.asset.status == "operational"
        assert not AssetStatusPeriod.objects.filter(asset=tenant_a.asset).exists()
        assert tenant_a.asset.plan_assignments.get().is_active


def test_foreign_detail_with_components_is_404(client, tenant_a, tenant_b, child_of_a):
    login(client, tenant_b.admin)
    url = reverse("asset_detail", args=[tenant_a.asset.pk])
    assert client.get(url, {"components": "1"}).status_code == 404


def test_other_org_never_sees_children_in_scope_or_kpis(tenant_a, tenant_b, child_of_a):
    with tenant_context(tenant_b.org):
        # Aun con el objeto de A en mano, desde B no aparecen sus componentes ni sus datos.
        assert asset_scope_ids(membership=tenant_b.admin, asset=tenant_a.asset, include_components=True) == [
            tenant_a.asset.pk
        ]
        kpis = asset_kpis(membership=tenant_b.admin, asset=tenant_a.asset, include_components=True)
        assert (kpis.availability_pct, kpis.failures, kpis.mttr_hours) == (None, 0, None)
        own = asset_kpis(membership=tenant_b.admin, asset=tenant_b.asset, include_components=True)
        assert own.failures == 0 and own.availability_pct == 100
