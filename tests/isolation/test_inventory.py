"""Aislamiento del inventario entre organizaciones y por sitio (INV-16, NF-07, ADR-026)."""

import uuid
from decimal import Decimal

import pytest
from django.core.exceptions import ObjectDoesNotExist
from django.db import DatabaseError, connection, transaction

from apps.accounts.models import MembershipLocation
from apps.core import rls
from apps.core.tenancy import CrossTenantError, tenant_context
from apps.inventory import services
from apps.inventory.models import Part, Reservation, StockItem, StockMovement, Warehouse
from apps.inventory.selectors import part_detail, stock_list, visible_warehouses, warehouse_for
from apps.inventory.services import InventoryError
from apps.organizations.models import Location
from tests.inventory_utils import make_work_order

pytestmark = pytest.mark.usefixtures("reconciled_inventory")
CROSS = (InventoryError, CrossTenantError, ObjectDoesNotExist)
INVENTORY_MODELS = (Part, Warehouse, StockItem, StockMovement, Reservation)


@pytest.fixture
def b_data(tenant_b):
    with tenant_context(tenant_b.org):
        part = services.create_part(membership=tenant_b.admin, code="ROD", name="Rodamiento B")
        warehouse = services.ensure_default_warehouse()
        wo = make_work_order(tenant_b)
        movement = services.receive_stock(membership=tenant_b.admin, part=part, quantity=5, unit_cost="10")
        out = services.issue_to_work_order(work_order=wo, part=part, quantity=1, by=tenant_b.tech.user)
        reservation = services.reserve_for_work_order(work_order=wo, part=part, quantity=2, source="manual")
    return {
        "part": part,
        "warehouse": warehouse,
        "wo": wo,
        "movement": movement,
        "out": out,
        "res": reservation,
    }


def test_services_reject_ids_of_another_organization(tenant_a, tenant_b, b_data):
    with tenant_context(tenant_a.org):
        a = tenant_a.admin
        own_part = services.create_part(
            membership=a, code="ROD", name="Rodamiento A"
        )  # mismo código, otra org
        own_wo = make_work_order(tenant_a)
        part_b, wh_b = b_data["part"], b_data["warehouse"]
        attempts = [
            lambda: services.receive_stock(membership=a, part=part_b, quantity=1),
            lambda: services.receive_stock(membership=a, part=own_part, quantity=1, warehouse=wh_b),
            lambda: services.count_stock(membership=a, part=part_b, counted_quantity=1),
            lambda: services.transfer_stock(
                membership=a,
                part=own_part,
                from_warehouse=services.ensure_default_warehouse(),
                to_warehouse=wh_b,
                quantity=1,
            ),
            lambda: services.set_stock_levels(membership=a, part=part_b, min_quantity=1),
            lambda: services.update_part(membership=a, part=part_b, name="hack"),
            lambda: services.set_part_active(membership=a, part=part_b, is_active=False),
            lambda: services.update_warehouse(membership=a, warehouse=wh_b, name="hack"),
            lambda: services.set_default_warehouse(membership=a, warehouse=wh_b),
            lambda: services.set_warehouse_active(membership=a, warehouse=wh_b, is_active=False),
            lambda: services.issue_to_work_order(work_order=own_wo, part=part_b, quantity=1, by=a.user),
            lambda: services.issue_to_work_order(
                work_order=b_data["wo"], part=own_part, quantity=1, by=a.user
            ),
            lambda: services.return_from_work_order(movement=b_data["out"], by=a.user),
            lambda: services.reserve_for_work_order(
                work_order=own_wo, part=part_b, quantity=1, source="manual"
            ),
            lambda: services.reserve_for_work_order(
                work_order=own_wo, part=own_part, quantity=1, warehouse=wh_b, source="manual"
            ),
            lambda: services.release_reservation(membership=a, reservation=b_data["res"]),
            lambda: services.apply_consumption_to_reservation(
                work_order=b_data["wo"], part=part_b, quantity=1
            ),
        ]
        for attempt in attempts:
            with pytest.raises(CROSS), transaction.atomic():
                attempt()
        with pytest.raises(CROSS):
            part_detail(part_b.pk, membership=a)
        assert [p.code for p in stock_list(membership=a)] == ["ROD"]
        assert stock_list(membership=a).get().pk == own_part.pk
        assert wh_b not in visible_warehouses(membership=a, include_inactive=True)
        assert warehouse_for(work_order=own_wo) != wh_b
        assert services.release_work_order_reservations(work_order=b_data["wo"], reason="closed") == 0
        assert services.reconcile_stock() == []
    with tenant_context(tenant_b.org):
        part_b.refresh_from_db()
        assert part_b.name == "Rodamiento B" and part_b.is_active
        assert StockItem.objects.get(part=part_b).on_hand == Decimal("4")
        assert Reservation.objects.get().status == "active"


def test_client_uuid_of_another_organization_does_not_collide(tenant_a, tenant_b, b_data):
    key = uuid.uuid4()
    with tenant_context(tenant_b.org):
        services.receive_stock(membership=tenant_b.admin, part=b_data["part"], quantity=1, client_uuid=key)
    with tenant_context(tenant_a.org):
        part = services.create_part(membership=tenant_a.admin, code="X", name="X")
        movement = services.receive_stock(membership=tenant_a.admin, part=part, quantity=1, client_uuid=key)
        assert movement.organization_id == tenant_a.org.pk and movement.part_id == part.pk


def test_restricted_membership_only_sees_and_moves_warehouses_in_scope(tenant_a):
    with tenant_context(tenant_a.org):
        north = Location.objects.create(code="N", name="Norte")
        south = Location.objects.create(code="S", name="Sur")
        line = Location.objects.create(code="NL", name="Línea norte", parent=north)
        main = services.ensure_default_warehouse()  # sin ubicación: la ven todos
        wh_north = services.create_warehouse(
            membership=tenant_a.admin, code="NOR", name="Norte", location=line
        )
        wh_south = services.create_warehouse(
            membership=tenant_a.admin, code="SUR", name="Sur", location=south
        )
        part = services.create_part(membership=tenant_a.admin, code="ROD", name="Rodamiento")
        services.receive_stock(membership=tenant_a.admin, part=part, quantity=3, warehouse=wh_south)
        admin = tenant_a.admin
        admin.restricted_to_locations = True
        admin.save()
        MembershipLocation.objects.create(membership=admin, location=north)
        assert set(visible_warehouses(membership=admin)) == {main, wh_north}
        assert stock_list(membership=admin).get().on_hand_total == 0
        with pytest.raises(InventoryError):
            services.receive_stock(membership=admin, part=part, quantity=1, warehouse=wh_south)
        with pytest.raises(InventoryError):
            services.transfer_stock(
                membership=admin, part=part, from_warehouse=wh_south, to_warehouse=wh_north, quantity=1
            )
        with pytest.raises(InventoryError):
            services.create_warehouse(membership=admin, code="X", name="X", location=south)
        assert part_detail(part.pk, membership=admin).stock_items == []


@pytest.mark.skipif(connection.vendor != "postgresql", reason="RLS solo existe en PostgreSQL")
def test_inventory_tables_have_rls(tenant_a, b_data):
    tables = set(rls.tenant_tables())
    assert {model._meta.db_table for model in INVENTORY_MODELS} <= tables
    assert rls.missing_policies() == []
    for model in INVENTORY_MODELS:
        table = connection.ops.quote_name(model._meta.db_table)
        with connection.cursor() as cursor:
            cursor.execute(f"SELECT COUNT(*) FROM {table}")
            assert cursor.fetchone()[0] == 0, model  # sin contexto no se ve nada
        with tenant_context(tenant_a.org), connection.cursor() as cursor:
            cursor.execute(f"SELECT COUNT(*) FROM {table}")
            assert cursor.fetchone()[0] == 0, model  # A no ve las filas de B
    with tenant_context(tenant_a.org):
        with pytest.raises(DatabaseError, match="row-level security"), transaction.atomic():
            Part.objects.bulk_create([Part(organization=b_data["part"].organization, code="Z", name="Z")])
