"""Permisos de inventario en la API v1 y la de terreno (INV-16): rol, alcance del token y sitio."""

import uuid

import pytest

from apps.accounts.models import Membership, MembershipLocation
from apps.core.tenancy import tenant_context
from apps.inventory import services as inventory
from apps.inventory.models import StockMovement
from apps.organizations.models import Location
from apps.work.models import PartUsage
from tests.api_utils import FIELD_API, api_client, field_client
from tests.inventory_utils import add_member, make_work_order

pytestmark = pytest.mark.usefixtures("reconciled_inventory")


@pytest.fixture
def part(tenant_a):
    with tenant_context(tenant_a.org):
        part = inventory.create_part(membership=tenant_a.admin, code="ROD", name="Rodamiento")
        inventory.receive_stock(membership=tenant_a.admin, part=part, quantity=5, unit_cost=100)
    return part


def test_read_scope_token_reads_inventory_but_cannot_write(tenant_a, part):
    client = api_client(tenant_a, scope="read")
    for url in ("parts/", "warehouses/", "stock/", "stock-movements/"):
        assert client.get(f"/api/v1/{url}").status_code == 200, url
    assert client.patch(f"/api/v1/parts/{part.pk}/", {"name": "x"}, format="json").status_code == 403
    body = {"kind": "count", "part": str(part.pk), "counted_quantity": "1", "note": "x"}
    assert client.post("/api/v1/stock-movements/", body, format="json").status_code == 403
    with tenant_context(tenant_a.org):
        assert StockMovement.objects.count() == 1


def test_token_acts_as_planner_and_sees_costs(tenant_a, part):
    """El principal del token tiene los permisos de planificador: gestiona y ve costos (Q3)."""
    client = api_client(tenant_a)
    assert client.get(f"/api/v1/parts/{part.pk}/").json()["avg_cost"] == "100.0000"
    body = {"kind": "count", "part": str(part.pk), "counted_quantity": "6", "note": "Conteo anual"}
    assert client.post("/api/v1/stock-movements/", body, format="json").status_code == 201


@pytest.mark.parametrize(
    ("role", "sees_parts"),
    [
        (Membership.Role.TECHNICIAN, True),
        (Membership.Role.SUPERVISOR, True),
        (Membership.Role.VIEWER, True),
        (Membership.Role.EXTERNAL, False),
    ],
)
def test_field_catalog_needs_inventory_view(tenant_a, part, role, sees_parts):
    membership = add_member(tenant_a, role, suffix="-inv")
    reference = field_client(membership).get(f"{FIELD_API}/reference/").json()
    assert bool(reference["parts"]) is sees_parts


def test_technician_does_not_see_inventory_costs_in_the_field_api(tenant_a, part):
    with tenant_context(tenant_a.org):
        wo = make_work_order(tenant_a)
    url = f"{FIELD_API}/work-orders/{wo.pk}/parts/"
    body = {"part": str(part.pk), "quantity": "1", "client_uuid": str(uuid.uuid4())}
    usage = field_client(tenant_a.tech).post(url, body, format="json").json()
    assert usage["unit_cost"] is None and usage["cost"] is None
    listed = field_client(tenant_a.viewer).get(url).json()["results"][0]
    assert listed["unit_cost"] == "100.0000"  # el visualizador sí ve costos de inventario


def test_viewer_cannot_use_parts_in_a_work_order(tenant_a, part):
    with tenant_context(tenant_a.org):
        wo = make_work_order(tenant_a)
    body = {"part": str(part.pk), "quantity": "1"}
    response = field_client(tenant_a.viewer).post(
        f"{FIELD_API}/work-orders/{wo.pk}/parts/", body, format="json"
    )
    assert response.status_code == 403
    with tenant_context(tenant_a.org):
        assert not PartUsage.objects.exists()


def test_scoped_technician_cannot_take_from_a_warehouse_outside_their_sites(tenant_a, part):
    with tenant_context(tenant_a.org):
        north = Location.objects.create(code="N", name="Planta Norte")
        south = Location.objects.create(code="S", name="Planta Sur")
        tenant_a.asset.location = north
        tenant_a.asset.save(update_fields=["location"])
        far = inventory.create_warehouse(
            membership=tenant_a.admin, code="SUR", name="Bodega Sur", location=south
        )
        MembershipLocation.objects.create(membership=tenant_a.tech, location=north)
        wo = make_work_order(tenant_a)
    tenant_a.tech.restricted_to_locations = True
    tenant_a.tech.save()
    body = {"part": str(part.pk), "quantity": "1", "warehouse": str(far.pk)}
    response = field_client(tenant_a.tech).post(
        f"{FIELD_API}/work-orders/{wo.pk}/parts/", body, format="json"
    )
    assert response.status_code == 400 and "warehouse" in response.json()["error"]["details"]
    # Sin elegir bodega, sale de la que corresponde (la por defecto, sin ubicación).
    del body["warehouse"]
    response = field_client(tenant_a.tech).post(
        f"{FIELD_API}/work-orders/{wo.pk}/parts/", body, format="json"
    )
    assert response.status_code == 201 and response.json()["warehouse"] != str(far.pk)
