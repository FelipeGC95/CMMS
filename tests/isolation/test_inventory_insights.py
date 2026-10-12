"""Aislamiento de los indicadores de inventario (INV-15) y de "Dónde se usa" (INV-01 CA4):
la organización A no ve valores, consumos, planes ni activos de B; el alcance por sitio
limita bodegas y activos."""

from decimal import Decimal

import pytest
from django.urls import reverse

from apps.accounts.models import MembershipLocation
from apps.assets.models import Asset
from apps.core.tenancy import tenant_context
from apps.inventory import services
from apps.inventory.selectors import inventory_insights, part_where_used
from apps.maintenance.services import create_plan
from apps.organizations.models import Location
from apps.work.services import add_part_usage
from tests.conftest import login
from tests.inventory_utils import make_work_order

pytestmark = pytest.mark.usefixtures("reconciled_inventory")


def _activity(tenant, *, cost):
    """Repuesto con existencia, un consumo en el activo del tenant y un plan que lo prevé."""
    with tenant_context(tenant.org):
        part = services.create_part(membership=tenant.admin, code="ROD", name=f"Rodamiento {tenant.org.slug}")
        services.receive_stock(membership=tenant.admin, part=part, quantity=10, unit_cost=cost)
        add_part_usage(work_order=make_work_order(tenant), membership=tenant.admin, part=part, quantity=1)
        create_plan(
            code="PMR",
            name=f"Plan {tenant.org.slug}",
            calendar_interval=30,
            items=[{"text": "x", "item_type": "check"}],
            resources=[
                {"kind": "part", "description": "Rodamiento", "quantity": 1, "unit": "un", "part": part}
            ],
        )
    return part


def test_insights_only_count_own_organization(tenant_a, tenant_b):
    _activity(tenant_a, cost="10")
    _activity(tenant_b, cost="1000")
    with tenant_context(tenant_a.org):
        insights = inventory_insights(membership=tenant_a.admin)
    assert insights.total_value == Decimal("90.00")  # 9 × 10; nada de B
    assert insights.consumption == Decimal("10.00")


def test_where_used_only_shows_own_plans_and_assets(client, tenant_a, tenant_b):
    part_a = _activity(tenant_a, cost="10")
    part_b = _activity(tenant_b, cost="10")
    with tenant_context(tenant_a.org):
        where = part_where_used(part_a, membership=tenant_a.admin)
    assert [use.plan.name for use in where.plans] == ["Plan empresa-a"]
    assert [use.code for use in where.assets] == ["CMP-01"]
    assert where.assets[0].asset_id == tenant_a.asset.pk
    login(client, tenant_a.admin)
    body = client.get(reverse("inventory_part_detail", args=[part_a.pk])).content.decode()
    assert "Plan empresa-b" not in body
    assert str(tenant_b.asset.pk) not in body
    assert client.get(reverse("inventory_part_detail", args=[part_b.pk])).status_code == 404


def test_site_scope_limits_insights_and_where_used(tenant_a):
    with tenant_context(tenant_a.org):
        a = tenant_a.admin
        north = Location.objects.create(code="N", name="Norte")
        south = Location.objects.create(code="S", name="Sur")
        services.ensure_default_warehouse()
        wh_south = services.create_warehouse(membership=a, code="SUR", name="Sur", location=south)
        part = services.create_part(membership=a, code="ROD", name="Rodamiento")
        services.receive_stock(membership=a, part=part, quantity=2, unit_cost="100")
        services.receive_stock(membership=a, part=part, quantity=5, unit_cost="100", warehouse=wh_south)
        north_asset = Asset.objects.create(
            code="N-01", name="Bomba norte", location=north, asset_class=tenant_a.asset.asset_class
        )
        south_asset = Asset.objects.create(
            code="S-01", name="Bomba sur", location=south, asset_class=tenant_a.asset.asset_class
        )
        for asset in (north_asset, south_asset):
            add_part_usage(
                work_order=make_work_order(tenant_a, asset=asset), membership=a, part=part, quantity=1
            )
        planner = a
        planner.restricted_to_locations = True
        planner.save()
        MembershipLocation.objects.create(membership=planner, location=north)
        insights = inventory_insights(membership=planner)
        where = part_where_used(part, membership=planner)
    # Ve la bodega sin ubicación (la principal), no la del sur.
    assert [row.code for row in insights.by_warehouse] == ["BOD"]
    assert [use.code for use in where.assets] == ["N-01"]
    assert insights.total_value == Decimal("100.00")  # 1 × 100 en la principal
    assert insights.consumption == Decimal("100.00")  # solo la salida de la bodega visible
