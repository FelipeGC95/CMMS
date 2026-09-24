"""Aislamiento entre organizaciones a nivel de modelo (NF-07). Obligatorio."""

import pytest
from django.apps import apps

from apps.assets.models import Asset, AssetClass
from apps.core.models import TenantModel
from apps.core.tenancy import CrossTenantError, TenantNotSet, tenant_context
from apps.work.services import create_work_order

TENANT_MODELS = [m for m in apps.get_models() if issubclass(m, TenantModel)]


def test_all_business_models_are_covered():
    names = {m.__name__ for m in TENANT_MODELS}
    assert {"Asset", "WorkOrder", "MaintenancePlan", "Location", "Attachment"} <= names


@pytest.mark.parametrize("model", TENANT_MODELS, ids=lambda m: m.__name__)
def test_default_manager_fails_closed_without_context(db, model):
    with pytest.raises(TenantNotSet):
        list(model.objects.all())


@pytest.mark.parametrize("model", TENANT_MODELS, ids=lambda m: m.__name__)
def test_default_manager_filters_by_organization(tenant_a, model):
    with tenant_context(tenant_a.org):
        sql = str(model.objects.all().query)
    assert "organization_id" in sql.split("WHERE", 1)[-1]


def test_org_b_does_not_see_org_a_rows(tenant_a, tenant_b):
    with tenant_context(tenant_b.org):
        assert list(Asset.objects.values_list("pk", flat=True)) == [tenant_b.asset.pk]
        assert not Asset.objects.filter(pk=tenant_a.asset.pk).exists()


def test_cannot_save_row_of_other_organization(tenant_a, tenant_b):
    with tenant_context(tenant_b.org), pytest.raises(CrossTenantError):
        tenant_a.asset.name = "hackeado"
        tenant_a.asset.save()


def test_cannot_point_foreign_key_to_other_organization(tenant_a, tenant_b):
    with tenant_context(tenant_b.org), pytest.raises(CrossTenantError):
        Asset.objects.create(
            code="X-1", name="X", asset_class=AssetClass.all_tenants.get(organization=tenant_a.org)
        )


def test_service_cannot_create_work_order_on_foreign_asset(tenant_a, tenant_b):
    with tenant_context(tenant_b.org), pytest.raises(CrossTenantError):
        create_work_order(work_type="corrective", title="x", asset=tenant_a.asset)
