"""Repuestos en la OT (INV-09, INV-10): la organización A no usa, reserva ni ve datos de B (INV-16)."""

import uuid

import pytest
from django.core.exceptions import ObjectDoesNotExist
from django.urls import reverse

from apps.accounts.models import MembershipLocation
from apps.core.tenancy import CrossTenantError, tenant_context
from apps.inventory import services as inventory
from apps.inventory.models import Reservation, StockMovement
from apps.inventory.services import InventoryError
from apps.organizations.models import Location
from apps.work.models import PartUsage
from apps.work.selectors import parts_for_work_order, planned_parts
from apps.work.services import add_part_usage, remove_cost_line
from tests.conftest import login
from tests.inventory_utils import make_work_order

pytestmark = pytest.mark.usefixtures("reconciled_inventory")
CROSS = (InventoryError, CrossTenantError, ObjectDoesNotExist)


@pytest.fixture
def b_data(tenant_b):
    with tenant_context(tenant_b.org):
        part = inventory.create_part(membership=tenant_b.admin, code="ROD", name="Rodamiento B")
        warehouse = inventory.ensure_default_warehouse()
        wo = make_work_order(tenant_b)
        reservation = inventory.reserve_for_work_order(work_order=wo, part=part, quantity=2, source="manual")
        usage = add_part_usage(work_order=wo, membership=tenant_b.tech, part=part, quantity=1)
    return {"part": part, "warehouse": warehouse, "wo": wo, "res": reservation, "usage": usage}


def test_services_reject_parts_and_warehouses_of_another_organization(tenant_a, b_data):
    with tenant_context(tenant_a.org):
        wo = make_work_order(tenant_a)
        own = inventory.create_part(membership=tenant_a.admin, code="ROD", name="Rodamiento A")
        with pytest.raises(CROSS):
            add_part_usage(work_order=wo, membership=tenant_a.tech, part=b_data["part"], quantity=1)
        with pytest.raises(CROSS):
            add_part_usage(work_order=wo, membership=tenant_a.tech, part=b_data["part"].pk, quantity=1)
        with pytest.raises(CROSS):
            add_part_usage(
                work_order=wo, membership=tenant_a.tech, part=own, quantity=1, warehouse=b_data["warehouse"]
            )
        with pytest.raises(CROSS):
            add_part_usage(work_order=b_data["wo"], membership=tenant_a.tech, part=own, quantity=1)
        with pytest.raises(CROSS):
            remove_cost_line(line=b_data["usage"], membership=tenant_a.tech)
        assert not PartUsage.objects.exists() and not StockMovement.objects.exists()


def test_client_uuid_of_another_organization_does_not_collide(tenant_a, tenant_b):
    key = uuid.uuid4()
    with tenant_context(tenant_b.org):
        part_b = inventory.create_part(membership=tenant_b.admin, code="ROD", name="B")
        add_part_usage(
            work_order=make_work_order(tenant_b),
            membership=tenant_b.tech,
            part=part_b,
            quantity=1,
            client_uuid=key,
        )
    with tenant_context(tenant_a.org):
        part_a = inventory.create_part(membership=tenant_a.admin, code="ROD", name="A")
        wo = make_work_order(tenant_a)
        usage = add_part_usage(
            work_order=wo, membership=tenant_a.tech, part=part_a, quantity=1, client_uuid=key
        )
        assert usage.work_order_id == wo.pk and usage.part_id == part_a.pk


def test_selectors_only_see_the_organization_of_the_context(tenant_a, b_data):
    with tenant_context(tenant_a.org):
        wo = make_work_order(tenant_a)
        assert parts_for_work_order(work_order=wo, membership=tenant_a.tech, q="ROD") == []
        assert planned_parts(work_order=wo) == []
        assert planned_parts(work_order=b_data["wo"]) == []


def test_views_reject_ids_of_another_organization(client, tenant_a, tenant_b, b_data):
    with tenant_context(tenant_a.org):
        wo = make_work_order(tenant_a)
    login(client, tenant_a.admin)
    client.post(
        reverse("work_order_part_add", args=[wo.pk]), {"part": str(b_data["part"].pk), "quantity": "1"}
    )
    client.post(
        reverse("inventory_wo_reserve", args=[wo.pk]), {"part": str(b_data["part"].pk), "quantity": "1"}
    )
    with tenant_context(tenant_a.org):
        assert not PartUsage.objects.exists() and not Reservation.objects.exists()
    # Rutas con la OT o la reserva de B: 404.
    b_wo, b_res = b_data["wo"], b_data["res"]
    assert client.post(reverse("inventory_wo_reserve", args=[b_wo.pk]), {"part": "x"}).status_code == 404
    assert client.post(reverse("inventory_wo_release", args=[b_wo.pk, b_res.pk])).status_code == 404
    assert client.post(reverse("inventory_wo_release", args=[wo.pk, b_res.pk])).status_code == 404
    assert client.get(reverse("inventory_wo_part_search", args=[b_wo.pk])).status_code == 404
    html = client.get(reverse("inventory_wo_part_search", args=[wo.pk]), {"part_q": "ROD"}).content.decode()
    assert "Rodamiento B" not in html
    with tenant_context(tenant_b.org):
        b_res.refresh_from_db()
        assert b_res.status == Reservation.Status.ACTIVE


def test_scoped_member_cannot_choose_a_warehouse_outside_their_sites(tenant_a):
    with tenant_context(tenant_a.org):
        mine = Location.objects.create(code="S1", name="Sitio 1")
        other = Location.objects.create(code="S2", name="Sitio 2")
        inventory.ensure_default_warehouse()
        hidden = inventory.create_warehouse(
            membership=tenant_a.admin, code="B2", name="Bodega 2", location=other
        )
        tenant_a.asset.location = mine
        tenant_a.asset.save()
        tech = tenant_a.tech
        tech.restricted_to_locations = True
        tech.save()
        MembershipLocation.objects.create(membership=tech, location=mine)
        part = inventory.create_part(membership=tenant_a.admin, code="ROD", name="Rodamiento")
        wo = make_work_order(tenant_a)
        with pytest.raises(InventoryError):
            add_part_usage(work_order=wo, membership=tech, part=part, quantity=1, warehouse=hidden)
        assert add_part_usage(work_order=wo, membership=tech, part=part, quantity=1).part_id == part.pk
