"""API v1 de inventario (INV-17) y repuestos en la OT (INV-09): contrato, idempotencia y errores."""

import uuid
from decimal import Decimal

import pytest

from apps.core.tenancy import tenant_context
from apps.inventory import services as inventory
from apps.inventory.models import Part, Reservation, StockItem, StockMovement
from apps.organizations.models import Plan
from apps.organizations.plans import BASE_MODULES
from apps.work.models import PartUsage, WorkOrder
from tests.api_utils import api_client
from tests.inventory_utils import make_work_order, set_status

pytestmark = pytest.mark.usefixtures("reconciled_inventory")

PART_KEYS = {
    "id",
    "code",
    "name",
    "description",
    "unit",
    "category",
    "manufacturer",
    "manufacturer_ref",
    "supplier",
    "notes",
    "is_active",
    "avg_cost",
    "last_cost",
    "last_received_at",
    "created_at",
    "updated_at",
}
MOVEMENT_KEYS = {
    "id",
    "part",
    "part_code",
    "warehouse",
    "warehouse_code",
    "kind",
    "reason",
    "quantity",
    "unit_cost",
    "total_cost",
    "avg_cost_after",
    "on_hand_after",
    "work_order",
    "reverses",
    "transfer_group",
    "reference",
    "supplier",
    "note",
    "occurred_at",
    "client_uuid",
    "created_at",
}
USAGE_KEYS = {
    "id",
    "work_order",
    "part",
    "part_code",
    "description",
    "quantity",
    "unit",
    "unit_cost",
    "cost",
    "warehouse",
    "stock_movement",
    "client_uuid",
    "created_at",
}


@pytest.fixture
def stock(tenant_a):
    """Dos repuestos y dos bodegas; 10 rodamientos a $1.000 en la principal."""
    with tenant_context(tenant_a.org):
        bearing = inventory.create_part(
            membership=tenant_a.admin, code="ROD-6204", name="Rodamiento 6204", category="Rodamientos"
        )
        belt = inventory.create_part(membership=tenant_a.admin, code="COR-A42", name="Correa A42")
        main = inventory.ensure_default_warehouse()
        other = inventory.create_warehouse(membership=tenant_a.admin, code="B2", name="Bodega 2")
        inventory.receive_stock(membership=tenant_a.admin, part=bearing, quantity=10, unit_cost=1000)
        inventory.set_stock_levels(membership=tenant_a.admin, part=belt, min_quantity=2)
    return {"bearing": bearing, "belt": belt, "main": main, "other": other}


def _plan(tenant, features):
    tenant.org.plan = Plan.objects.create(code="plan-" + "-".join(features), name="Plan", features=features)
    tenant.org.save(update_fields=["plan"])


def _on_hand(tenant, part, warehouse):
    with tenant_context(tenant.org):
        return StockItem.objects.get(part=part, warehouse=warehouse).on_hand


# --- Repuestos -----------------------------------------------------------------------------


def test_parts_list_detail_and_filters(tenant_a, stock):
    client = api_client(tenant_a)
    body = client.get("/api/v1/parts/").json()
    assert body["count"] == 2
    row = body["results"][1]
    assert set(row) == PART_KEYS
    assert row["code"] == "ROD-6204" and row["avg_cost"] == "1000.0000"  # el token ve costos
    assert client.get("/api/v1/parts/", {"code": "rod-6204"}).json()["count"] == 1
    assert client.get("/api/v1/parts/", {"search": "correa"}).json()["results"][0]["code"] == "COR-A42"
    assert client.get("/api/v1/parts/", {"category": "rodamientos"}).json()["count"] == 1
    detail = client.get(f"/api/v1/parts/{stock['bearing'].pk}/")
    assert detail.status_code == 200 and detail.json()["name"] == "Rodamiento 6204"


def test_part_create_is_idempotent_by_code(tenant_a, stock):
    client = api_client(tenant_a)
    response = client.post(
        "/api/v1/parts/", {"code": "FIL-01", "name": "Filtro", "unit": "un"}, format="json"
    )
    assert response.status_code == 201
    created = response.json()
    assert set(created) == PART_KEYS and created["unit"] == "un"
    again = client.post("/api/v1/parts/", {"code": "fil-01", "name": "Otro nombre"}, format="json")
    assert again.status_code == 200
    assert again.json()["id"] == created["id"] and again.json()["name"] == "Filtro"  # sin cambios
    with tenant_context(tenant_a.org):
        assert Part.objects.filter(code__iexact="FIL-01").count() == 1


def test_part_create_validation_errors(tenant_a, stock):
    response = api_client(tenant_a).post("/api/v1/parts/", {"name": "Sin código"}, format="json")
    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == "invalid" and "code" in error["details"]


def test_part_patch_updates_and_deactivates(tenant_a, stock):
    client = api_client(tenant_a)
    url = f"/api/v1/parts/{stock['belt'].pk}/"
    response = client.patch(url, {"name": "Correa A-42", "supplier": "Proveedor X"}, format="json")
    assert response.status_code == 200
    assert response.json()["name"] == "Correa A-42" and response.json()["supplier"] == "Proveedor X"
    duplicate = client.patch(url, {"code": "rod-6204"}, format="json")
    assert duplicate.status_code == 400 and "code" in duplicate.json()["error"]["details"]
    response = client.patch(url, {"is_active": False}, format="json")
    assert response.status_code == 200 and response.json()["is_active"] is False
    assert client.get("/api/v1/parts/").json()["count"] == 1
    assert client.get("/api/v1/parts/", {"include_inactive": "true"}).json()["count"] == 2
    assert client.put(url, {"code": "X", "name": "Y"}, format="json").status_code == 405


def test_costs_are_hidden_without_cost_permission(tenant_a, stock, monkeypatch):
    monkeypatch.setattr("apps.api.v1.show_inventory_costs", lambda membership: False)
    client = api_client(tenant_a)
    part = client.get(f"/api/v1/parts/{stock['bearing'].pk}/").json()
    assert part["avg_cost"] is None and part["last_cost"] is None
    (movement,) = client.get("/api/v1/stock-movements/").json()["results"]
    assert movement["unit_cost"] is None and movement["total_cost"] is None
    assert movement["avg_cost_after"] is None and movement["quantity"] == "10.000"


# --- Bodegas y stock ------------------------------------------------------------------------


def test_warehouses_and_stock_with_filters(tenant_a, stock):
    client = api_client(tenant_a)
    warehouses = client.get("/api/v1/warehouses/").json()["results"]
    assert [w["code"] for w in warehouses] == ["BOD", "B2"] and warehouses[0]["is_default"] is True
    rows = client.get("/api/v1/stock/").json()["results"]
    assert {(r["part_code"], r["on_hand"]) for r in rows} == {("ROD-6204", "10.000"), ("COR-A42", "0.000")}
    bearing = client.get("/api/v1/stock/", {"part": str(stock["bearing"].pk)}).json()["results"]
    assert len(bearing) == 1 and bearing[0]["available"] == "10.000" and bearing[0]["below_min"] is False
    below = client.get("/api/v1/stock/", {"below_min": "true"}).json()["results"]
    assert [r["part_code"] for r in below] == ["COR-A42"] and below[0]["below_min"] is True
    assert client.get("/api/v1/stock/", {"warehouse": str(stock["other"].pk)}).json()["count"] == 0
    assert client.get("/api/v1/stock/", {"part": "no-es-uuid"}).status_code == 400


# --- Movimientos ----------------------------------------------------------------------------


def test_receipt_is_idempotent_by_client_uuid(tenant_a, stock):
    client = api_client(tenant_a)
    key = str(uuid.uuid4())
    body = {
        "kind": "receipt",
        "part": str(stock["bearing"].pk),
        "quantity": "10",
        "unit_cost": "1200",
        "reference": "FAC-123",
        "client_uuid": key,
    }
    response = client.post("/api/v1/stock-movements/", body, format="json")
    assert response.status_code == 201
    (movement,) = response.json()["movements"]
    assert set(movement) == MOVEMENT_KEYS
    assert movement["kind"] == "receipt" and movement["avg_cost_after"] == "1100.0000"
    again = client.post("/api/v1/stock-movements/", body, format="json")
    assert again.status_code == 200 and again.json()["movements"][0]["id"] == movement["id"]
    assert _on_hand(tenant_a, stock["bearing"], stock["main"]) == Decimal(20)
    # La misma clave en otra operación es un conflicto.
    count = {"kind": "count", "part": str(stock["bearing"].pk), "counted_quantity": "5", "client_uuid": key}
    conflict = client.post("/api/v1/stock-movements/", count, format="json")
    assert conflict.status_code == 409 and conflict.json()["error"]["code"] == "conflict"


def test_count_adjusts_or_reports_no_difference(tenant_a, stock):
    client = api_client(tenant_a)
    same = {"kind": "count", "part": str(stock["bearing"].pk), "counted_quantity": "10"}
    response = client.post("/api/v1/stock-movements/", same, format="json")
    assert response.status_code == 200 and response.json() == {"movements": []}
    lower = {**same, "counted_quantity": "8"}
    response = client.post("/api/v1/stock-movements/", lower, format="json")
    assert response.status_code == 400 and "note" in response.json()["error"]["details"]
    response = client.post("/api/v1/stock-movements/", {**lower, "note": "Merma"}, format="json")
    assert response.status_code == 201
    (movement,) = response.json()["movements"]
    assert movement["kind"] == "adjustment" and movement["quantity"] == "-2.000"


def test_transfer_creates_two_linked_movements(tenant_a, stock):
    client = api_client(tenant_a)
    body = {
        "kind": "transfer",
        "part": str(stock["bearing"].pk),
        "quantity": "3",
        "warehouse": str(stock["main"].pk),
        "to_warehouse": str(stock["other"].pk),
        "client_uuid": str(uuid.uuid4()),
    }
    response = client.post("/api/v1/stock-movements/", body, format="json")
    assert response.status_code == 201
    out, inbound = response.json()["movements"]
    assert (out["kind"], inbound["kind"]) == ("transfer_out", "transfer_in")
    assert out["transfer_group"] == inbound["transfer_group"]
    again = client.post("/api/v1/stock-movements/", body, format="json")
    assert again.status_code == 200 and [m["id"] for m in again.json()["movements"]] == [
        out["id"],
        inbound["id"],
    ]
    assert _on_hand(tenant_a, stock["bearing"], stock["other"]) == Decimal(3)
    missing = client.post("/api/v1/stock-movements/", {**body, "to_warehouse": None}, format="json")
    assert missing.status_code == 400 and "to_warehouse" in missing.json()["error"]["details"]
    listed = client.get("/api/v1/stock-movements/", {"kind": "transfer_in"}).json()["results"]
    assert [m["id"] for m in listed] == [inbound["id"]]


def test_movement_validation(tenant_a, stock):
    client = api_client(tenant_a)
    part = str(stock["bearing"].pk)
    for body, field in (
        ({"kind": "receipt", "part": part}, "quantity"),
        ({"kind": "count", "part": part, "quantity": "1"}, "counted_quantity"),
        ({"kind": "issue", "part": part, "quantity": "1"}, "kind"),
        ({"kind": "receipt", "part": str(uuid.uuid4()), "quantity": "1"}, "part"),
        ({"kind": "receipt", "part": part, "quantity": "0"}, "quantity"),
    ):
        response = client.post("/api/v1/stock-movements/", body, format="json")
        assert response.status_code == 400, body
        assert field in response.json()["error"]["details"], body


def test_inactive_part_cannot_move(tenant_a, stock):
    with tenant_context(tenant_a.org):
        inventory.set_part_active(membership=tenant_a.admin, part=stock["belt"], is_active=False)
    body = {"kind": "receipt", "part": str(stock["belt"].pk), "quantity": "1"}
    response = api_client(tenant_a).post("/api/v1/stock-movements/", body, format="json")
    assert response.status_code == 400 and "part" in response.json()["error"]["details"]


# --- Repuestos en la OT ---------------------------------------------------------------------


def test_work_order_parts_from_catalog_consume_stock_and_reservation(tenant_a, stock):
    with tenant_context(tenant_a.org):
        wo = make_work_order(tenant_a)
        inventory.reserve_for_work_order(work_order=wo, part=stock["bearing"], quantity=2, source="manual")
    client = api_client(tenant_a)
    url = f"/api/v1/work-orders/{wo.pk}/parts/"
    key = str(uuid.uuid4())
    body = {"part": str(stock["bearing"].pk), "quantity": "2", "unit_cost": "1", "client_uuid": key}
    response = client.post(url, body, format="json")
    assert response.status_code == 201
    usage = response.json()
    assert set(usage) == USAGE_KEYS
    assert usage["description"] == "ROD-6204 · Rodamiento 6204" and usage["part_code"] == "ROD-6204"
    assert usage["unit_cost"] == "1000.0000" and usage["cost"] == "2000.00"  # costo del servidor
    assert usage["warehouse"] == str(stock["main"].pk)
    again = client.post(url, body, format="json")
    assert again.status_code == 200 and again.json()["id"] == usage["id"]
    assert _on_hand(tenant_a, stock["bearing"], stock["main"]) == Decimal(8)
    with tenant_context(tenant_a.org):
        assert WorkOrder.objects.get(pk=wo.pk).total_cost == Decimal("2000.00")
        assert Reservation.objects.get(work_order=wo).status == Reservation.Status.FULFILLED
        assert PartUsage.objects.filter(work_order=wo).count() == 1
    listed = client.get(url).json()
    assert listed["count"] == 1 and listed["results"][0]["id"] == usage["id"]


def test_work_order_parts_free_text_needs_description_and_cost(tenant_a, stock):
    with tenant_context(tenant_a.org):
        wo = make_work_order(tenant_a)
    client = api_client(tenant_a)
    url = f"/api/v1/work-orders/{wo.pk}/parts/"
    response = client.post(url, {"quantity": "1"}, format="json")
    assert response.status_code == 400 and "description" in response.json()["error"]["details"]
    response = client.post(url, {"description": "Huaipe", "quantity": "1"}, format="json")
    assert response.status_code == 400 and "unit_cost" in response.json()["error"]["details"]
    response = client.post(url, {"description": "Huaipe", "quantity": "2", "unit_cost": "500"}, format="json")
    assert response.status_code == 201
    assert response.json()["part"] is None and response.json()["cost"] == "1000.00"


def test_work_order_parts_conflicts_and_state_errors(tenant_a, stock):
    with tenant_context(tenant_a.org):
        first, second = make_work_order(tenant_a), make_work_order(tenant_a, title="Otra")
    client = api_client(tenant_a)
    key = str(uuid.uuid4())
    body = {"part": str(stock["bearing"].pk), "quantity": "1", "client_uuid": key}
    assert client.post(f"/api/v1/work-orders/{first.pk}/parts/", body, format="json").status_code == 201
    other = client.post(f"/api/v1/work-orders/{second.pk}/parts/", body, format="json")
    assert other.status_code == 409 and other.json()["error"]["code"] == "conflict"
    # Una clave ya usada por un movimiento de otro tipo también choca.
    receipt = {"kind": "receipt", "part": str(stock["bearing"].pk), "quantity": "1", "client_uuid": key}
    assert client.post("/api/v1/stock-movements/", receipt, format="json").status_code == 409
    # Como el resto de la v1, una OT cancelada o cerrada responde 400 (la de terreno, 409).
    with tenant_context(tenant_a.org):
        set_status(second, WorkOrder.Status.CANCELLED)
    fresh = {**body, "client_uuid": str(uuid.uuid4())}
    response = client.post(f"/api/v1/work-orders/{second.pk}/parts/", fresh, format="json")
    assert response.status_code == 400
    # El reenvío de la línea ya creada responde igual aunque la OT ya esté cerrada.
    with tenant_context(tenant_a.org):
        set_status(first, WorkOrder.Status.CLOSED)
    assert client.post(f"/api/v1/work-orders/{first.pk}/parts/", body, format="json").status_code == 200


def test_negative_stock_is_allowed(tenant_a, stock):
    with tenant_context(tenant_a.org):
        wo = make_work_order(tenant_a)
    body = {"part": str(stock["belt"].pk), "quantity": "3"}
    response = api_client(tenant_a).post(f"/api/v1/work-orders/{wo.pk}/parts/", body, format="json")
    assert response.status_code == 201
    assert _on_hand(tenant_a, stock["belt"], stock["main"]) == Decimal(-3)


# --- Módulo y alcance del token -------------------------------------------------------------


def test_plan_without_inventory_blocks_the_inventory_api(tenant_a, stock):
    _plan(tenant_a, [*BASE_MODULES, "api"])
    client = api_client(tenant_a)
    for url in ("parts/", "warehouses/", "stock/", "stock-movements/"):
        response = client.get(f"/api/v1/{url}")
        assert response.status_code == 403, url
        assert "Inventario y repuestos" in response.json()["error"]["message"]
    with tenant_context(tenant_a.org):
        wo = make_work_order(tenant_a)
    url = f"/api/v1/work-orders/{wo.pk}/parts/"
    response = client.post(url, {"part": str(stock["bearing"].pk), "quantity": "1"}, format="json")
    assert response.status_code == 403 and "Inventario y repuestos" in response.json()["error"]["message"]
    # El texto libre sigue disponible.
    body = {"description": "Huaipe", "quantity": "1", "unit_cost": "100"}
    assert client.post(url, body, format="json").status_code == 201


def test_read_scope_token_cannot_write_inventory(tenant_a, stock):
    client = api_client(tenant_a, scope="read")
    assert client.get("/api/v1/parts/").status_code == 200
    assert client.post("/api/v1/parts/", {"code": "X", "name": "Y"}, format="json").status_code == 403
    body = {"kind": "receipt", "part": str(stock["bearing"].pk), "quantity": "1"}
    assert client.post("/api/v1/stock-movements/", body, format="json").status_code == 403
    with tenant_context(tenant_a.org):
        wo = make_work_order(tenant_a)
    body = {"part": str(stock["bearing"].pk), "quantity": "1"}
    assert client.post(f"/api/v1/work-orders/{wo.pk}/parts/", body, format="json").status_code == 403
    with tenant_context(tenant_a.org):
        assert not StockMovement.objects.filter(kind="issue").exists()
