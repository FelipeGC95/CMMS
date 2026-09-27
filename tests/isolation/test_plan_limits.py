"""ORG-10: el cupo y los módulos de una organización no dependen de los datos ni del plan de otra."""

from django.urls import reverse

from apps.assets.services import create_asset
from apps.core.tenancy import tenant_context
from apps.organizations.models import Plan
from apps.organizations.plans import BASE_MODULES
from tests.conftest import login


def test_quota_counts_only_own_assets(tenant_a, tenant_b):
    plan = Plan.objects.create(code="dos", name="Dos", max_assets=2, features=BASE_MODULES)
    for tenant in (tenant_a, tenant_b):
        tenant.org.plan = plan
        tenant.org.save(update_fields=["plan"])
    # B tiene 1 activo; A crea su segundo sin que cuenten los de B.
    with tenant_context(tenant_a.org):
        create_asset(membership=tenant_a.admin, code="A-2", name="A2", asset_class=tenant_a.asset.asset_class)
    with tenant_context(tenant_b.org):
        create_asset(membership=tenant_b.admin, code="B-2", name="B2", asset_class=tenant_b.asset.asset_class)


def test_module_gating_uses_active_organization_plan(client, tenant_a, tenant_b):
    tenant_b.org.plan = Plan.objects.create(code="sin-prev", name="Sin preventivo", features=["assets"])
    tenant_b.org.save(update_fields=["plan"])
    login(client, tenant_a.admin)
    assert client.get(reverse("plan_list")).status_code == 200
