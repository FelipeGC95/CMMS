"""Aislamiento de los costos de OT entre organizaciones (NF-07)."""

from decimal import Decimal

from django.urls import reverse

from apps.core.tenancy import tenant_context
from apps.work.models import LaborRate, PartUsage
from apps.work.services import add_part_usage, create_work_order, hourly_cost_for
from tests.conftest import login


def test_user_of_b_cannot_add_or_remove_costs_on_a(client, tenant_a, tenant_b):
    with tenant_context(tenant_a.org):
        wo = create_work_order(work_type="corrective", title="x", asset=tenant_a.asset)
        part = add_part_usage(
            work_order=wo, membership=tenant_a.tech, description="Filtro", quantity=1, unit_cost=10
        )
    login(client, tenant_b.admin)
    part_data = {"description": "x", "quantity": "1", "unit_cost": "1"}
    assert client.post(reverse("work_order_part_add", args=[wo.pk]), part_data).status_code == 404
    assert client.post(reverse("work_order_labor_add", args=[wo.pk]), {"minutes": "5"}).status_code == 404
    other = {"description": "x", "amount": "1"}
    assert client.post(reverse("work_order_other_cost_add", args=[wo.pk]), other).status_code == 404
    delete = reverse("work_order_cost_delete", args=[wo.pk, "part", part.pk])
    assert client.post(delete).status_code == 404
    with tenant_context(tenant_a.org):
        assert PartUsage.objects.count() == 1


def test_labor_rates_do_not_cross_organizations(tenant_a, tenant_b):
    with tenant_context(tenant_a.org):
        LaborRate.objects.create(hourly_cost=Decimal("50000"))
    with tenant_context(tenant_b.org):
        assert hourly_cost_for(tenant_b.tech) == 0
