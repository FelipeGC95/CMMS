"""Inventario en la API v1 y en la de terreno (INV-16/17): A nunca lee, escribe ni referencia datos de B."""

import uuid

import pytest

from apps.core.tenancy import tenant_context
from apps.inventory import services as inventory
from apps.inventory.models import Part, StockMovement
from apps.work.models import PartUsage
from tests.api_utils import FIELD_API, api_client, field_client
from tests.inventory_utils import make_work_order

pytestmark = pytest.mark.usefixtures("reconciled_inventory")


def _inventory(tenant, code):
    with tenant_context(tenant.org):
        part = inventory.create_part(membership=tenant.admin, code=code, name=f"Repuesto {code}")
        warehouse = inventory.ensure_default_warehouse()
        movement = inventory.receive_stock(membership=tenant.admin, part=part, quantity=5, unit_cost=100)
        work_order = make_work_order(tenant)
        inventory.reserve_for_work_order(work_order=work_order, part=part, quantity=1, source="manual")
        stock_item = part.stock_items.get()
    return {
        "parts": part.pk,
        "warehouses": warehouse.pk,
        "stock": stock_item.pk,
        "stock-movements": movement.pk,
        "part": part,
        "warehouse": warehouse,
        "work_order": work_order,
    }


@pytest.fixture
def a_data(tenant_a):
    return _inventory(tenant_a, "ROD-A")


@pytest.fixture
def b_data(tenant_b):
    return _inventory(tenant_b, "ROD-B")


@pytest.mark.parametrize("resource", ["parts", "warehouses", "stock", "stock-movements"])
def test_lists_and_details_only_show_own_organization(tenant_a, a_data, b_data, resource):
    client = api_client(tenant_a)
    ids = {row["id"] for row in client.get(f"/api/v1/{resource}/").json()["results"]}
    assert ids and str(b_data[resource]) not in ids
    assert client.get(f"/api/v1/{resource}/{b_data[resource]}/").status_code == 404


def test_filters_by_other_organization_ids_return_nothing(tenant_a, a_data, b_data):
    client = api_client(tenant_a)
    for resource, name in (
        ("stock", "part"),
        ("stock", "warehouse"),
        ("stock-movements", "part"),
        ("stock-movements", "warehouse"),
    ):
        response = client.get(f"/api/v1/{resource}/", {name: str(b_data[name].pk)})
        assert response.json()["count"] == 0, (resource, name)
    work_order = str(b_data["work_order"].pk)
    assert client.get("/api/v1/stock-movements/", {"work_order": work_order}).json()["count"] == 0
    assert client.get("/api/v1/parts/", {"code": "ROD-B"}).json()["count"] == 0


def test_writes_cannot_touch_or_reference_other_organization(tenant_a, a_data, b_data):
    client = api_client(tenant_a)
    part_b, warehouse_b = str(b_data["part"].pk), str(b_data["warehouse"].pk)
    part_a, warehouse_a = str(a_data["part"].pk), str(a_data["warehouse"].pk)
    assert client.patch(f"/api/v1/parts/{part_b}/", {"name": "x"}, format="json").status_code == 404
    for body, field in (
        ({"kind": "receipt", "part": part_b, "quantity": "1"}, "part"),
        ({"kind": "receipt", "part": part_a, "quantity": "1", "warehouse": warehouse_b}, "warehouse"),
        ({"kind": "count", "part": part_a, "counted_quantity": "1", "warehouse": warehouse_b}, "warehouse"),
        (
            {
                "kind": "transfer",
                "part": part_a,
                "quantity": "1",
                "warehouse": warehouse_a,
                "to_warehouse": warehouse_b,
            },
            "to_warehouse",
        ),
    ):
        response = client.post("/api/v1/stock-movements/", body, format="json")
        assert response.status_code == 400 and field in response.json()["error"]["details"], body
    wo_a, wo_b = a_data["work_order"], b_data["work_order"]
    body = {"part": part_a, "quantity": "1"}
    assert client.post(f"/api/v1/work-orders/{wo_b.pk}/parts/", body, format="json").status_code == 404
    assert client.get(f"/api/v1/work-orders/{wo_b.pk}/parts/").status_code == 404
    for body, field in (
        ({"part": part_b, "quantity": "1"}, "part"),
        ({"part": part_a, "quantity": "1", "warehouse": warehouse_b}, "warehouse"),
    ):
        response = client.post(f"/api/v1/work-orders/{wo_a.pk}/parts/", body, format="json")
        assert response.status_code == 400 and field in response.json()["error"]["details"], body
    with tenant_context(b_data["part"].organization):
        assert StockMovement.objects.count() == 1 and not PartUsage.objects.exists()
        assert Part.objects.get(pk=b_data["part"].pk).name == "Repuesto ROD-B"


def test_part_code_of_other_organization_does_not_block_or_return_it(tenant_a, b_data):
    response = api_client(tenant_a).post("/api/v1/parts/", {"code": "rod-b", "name": "Mío"}, format="json")
    assert response.status_code == 201 and response.json()["id"] != str(b_data["part"].pk)


def test_client_uuid_of_other_organization_does_not_collide(tenant_a, tenant_b, a_data, b_data):
    key = str(uuid.uuid4())
    for tenant, data in ((tenant_a, a_data), (tenant_b, b_data)):
        client = api_client(tenant)
        body = {"kind": "receipt", "part": str(data["part"].pk), "quantity": "1", "client_uuid": key}
        response = client.post("/api/v1/stock-movements/", body, format="json")
        assert response.status_code == 201, tenant.org.slug
    usage_key = str(uuid.uuid4())
    for tenant, data in ((tenant_a, a_data), (tenant_b, b_data)):
        usage = {"part": str(data["part"].pk), "quantity": "1", "client_uuid": usage_key}
        url = f"/api/v1/work-orders/{data['work_order'].pk}/parts/"
        response = api_client(tenant).post(url, usage, format="json")
        assert response.status_code == 201, tenant.org.slug


def test_field_reads_and_writes_stay_in_the_active_organization(tenant_a, tenant_b, a_data, b_data):
    client = field_client(tenant_b.tech)
    reference = client.get(f"{FIELD_API}/reference/").json()
    assert [p["code"] for p in reference["parts"]] == ["ROD-B"]
    snapshot = client.get(f"{FIELD_API}/snapshot/").json()["work_orders"]
    planned = {p["part"] for wo in snapshot for p in wo["planned_parts"]}
    assert planned == {str(b_data["part"].pk)}
    wo_a, wo_b = a_data["work_order"], b_data["work_order"]
    body = {"part": str(b_data["part"].pk), "quantity": "1"}
    assert client.post(f"{FIELD_API}/work-orders/{wo_a.pk}/parts/", body, format="json").status_code == 404
    for body, field in (
        ({"part": str(a_data["part"].pk), "quantity": "1"}, "part"),
        (
            {"part": str(b_data["part"].pk), "quantity": "1", "warehouse": str(a_data["warehouse"].pk)},
            "warehouse",
        ),
    ):
        response = client.post(f"{FIELD_API}/work-orders/{wo_b.pk}/parts/", body, format="json")
        assert response.status_code == 400 and field in response.json()["error"]["details"], body
    for tenant in (tenant_a, tenant_b):
        with tenant_context(tenant.org):
            assert not PartUsage.objects.exists()
