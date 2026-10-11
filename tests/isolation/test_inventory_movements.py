"""Aislamiento de las pantallas de movimientos, reposición y Excel de inventario (INV-16 CA3): IDs de
otra organización en URLs y formularios no se encuentran, y las listas solo muestran lo propio y
lo que cae en el alcance por sitio (INV-03 CA5)."""

import uuid

import pytest
from django.urls import reverse

from apps.accounts.models import MembershipLocation
from apps.core.tenancy import tenant_context
from apps.inventory import services
from apps.inventory.models import StockItem, StockMovement, Warehouse
from apps.organizations.models import Location
from tests.conftest import login
from tests.test_exports_excel import xlsx_rows

pytestmark = pytest.mark.usefixtures("reconciled_inventory")
B_KEY = uuid.UUID("01900000-0000-7000-8000-00000000000b")


@pytest.fixture
def b_data(tenant_b):
    with tenant_context(tenant_b.org):
        a = tenant_b.admin
        part = services.create_part(membership=a, code="ROD", name="Rodamiento de B")
        main = services.ensure_default_warehouse()
        other = services.create_warehouse(membership=a, code="BNORTE", name="Norte de B")
        services.receive_stock(
            membership=a, part=part, quantity=5, unit_cost="10", reference="DOC-DE-B", client_uuid=B_KEY
        )
        services.set_stock_levels(membership=a, part=part, min_quantity=50)  # B por reponer
    return {"part": part, "main": main, "other": other}


@pytest.fixture
def a_data(tenant_a):
    with tenant_context(tenant_a.org):
        a = tenant_a.admin
        part = services.create_part(membership=a, code="ROD", name="Rodamiento de A")
        main = services.ensure_default_warehouse()
        second = services.create_warehouse(membership=a, code="SEG", name="Segunda de A")
        services.receive_stock(membership=a, part=part, quantity=2, reference="DOC-DE-A")
        services.set_stock_levels(membership=a, part=part, min_quantity=5)
    return {"part": part, "main": main, "second": second}


def _b_untouched(b_data):
    with tenant_context(b_data["part"].organization):
        assert StockMovement.objects.count() == 1
        assert StockItem.objects.get(part=b_data["part"], warehouse=b_data["main"]).on_hand == 5


def _a_movements(tenant_a):
    with tenant_context(tenant_a.org):
        return StockMovement.objects.count()


def test_receive_with_a_part_of_another_organization_is_not_found(client, tenant_a, a_data, b_data):
    login(client, tenant_a.admin)
    body = client.get(reverse("inventory_receive"), {"part": b_data["part"].pk}).content.decode()
    assert "Rodamiento de B" not in body and f'value="{b_data["part"].pk}"' not in body
    response = client.post(
        reverse("inventory_receive"),
        {
            "part": b_data["part"].pk,
            "quantity": "3",
            "warehouse": a_data["main"].pk,
            "client_uuid": uuid.uuid4(),
        },
    )
    assert response.status_code == 200 and "Repuesto no encontrado." in response.content.decode()
    assert _a_movements(tenant_a) == 1
    _b_untouched(b_data)


@pytest.mark.parametrize("warehouse", ["main", "other"])
def test_receive_into_a_warehouse_of_another_organization_is_rejected(
    client, tenant_a, a_data, b_data, warehouse
):
    login(client, tenant_a.admin)
    response = client.post(
        reverse("inventory_receive"),
        {
            "part": a_data["part"].pk,
            "quantity": "3",
            "warehouse": b_data[warehouse].pk,
            "client_uuid": uuid.uuid4(),
        },
    )
    assert response.status_code == 200 and response.context["form"].errors["warehouse"]
    assert _a_movements(tenant_a) == 1
    _b_untouched(b_data)


@pytest.mark.parametrize("method", ["get", "post"])
def test_count_of_another_organization_part_is_404(client, tenant_a, a_data, b_data, method):
    login(client, tenant_a.admin)
    url = reverse("inventory_part_count", args=[b_data["part"].pk])
    data = {"counted_quantity": "0", "note": "x", "client_uuid": uuid.uuid4()}
    response = client.post(url, data) if method == "post" else client.get(url)
    assert response.status_code == 404
    assert client.get(reverse("inventory_count"), {"part": b_data["part"].pk}).status_code == 200
    _b_untouched(b_data)


def test_count_into_a_warehouse_of_another_organization_is_rejected(client, tenant_a, a_data, b_data):
    login(client, tenant_a.admin)
    response = client.post(
        reverse("inventory_part_count", args=[a_data["part"].pk]),
        {"counted_quantity": "0", "note": "x", "warehouse": b_data["main"].pk, "client_uuid": uuid.uuid4()},
    )
    assert response.status_code == 200 and response.context["form"].errors["warehouse"]
    _b_untouched(b_data)


def test_transfer_with_ids_of_another_organization_is_rejected(client, tenant_a, a_data, b_data):
    login(client, tenant_a.admin)
    url = reverse("inventory_transfer")
    cases = [
        {"part": b_data["part"].pk, "from_warehouse": a_data["main"].pk, "to_warehouse": a_data["second"].pk},
        {"part": a_data["part"].pk, "from_warehouse": b_data["main"].pk, "to_warehouse": a_data["second"].pk},
        {"part": a_data["part"].pk, "from_warehouse": a_data["main"].pk, "to_warehouse": b_data["other"].pk},
    ]
    for data in cases:
        response = client.post(url, {**data, "quantity": "1", "client_uuid": uuid.uuid4()})
        assert response.status_code == 200, data
    assert _a_movements(tenant_a) == 1
    _b_untouched(b_data)


def test_client_uuid_of_another_organization_does_not_collide(client, tenant_a, a_data, b_data):
    with tenant_context(b_data["part"].organization):
        key = StockMovement.objects.get().client_uuid
    login(client, tenant_a.admin)
    data = {"part": a_data["part"].pk, "quantity": "1", "warehouse": a_data["main"].pk, "client_uuid": key}
    assert client.post(reverse("inventory_receive"), data).status_code == 302
    assert _a_movements(tenant_a) == 2
    _b_untouched(b_data)


def test_lists_show_only_the_own_organization(client, tenant_a, a_data, b_data):
    login(client, tenant_a.admin)
    movements = client.get(reverse("inventory_movements")).content.decode()
    assert "DOC-DE-A" in movements and "DOC-DE-B" not in movements
    for params in ({"warehouse": b_data["main"].pk}, {"part": "ROD"}):
        response = client.get(reverse("inventory_movements"), params)
        assert response.status_code == 200
        assert "DOC-DE-B" not in response.content.decode()
    reorder = client.get(reverse("inventory_reorder"))
    assert [item.part_id for item in reorder.context["page"].object_list] == [a_data["part"].pk]
    reorder = client.get(reverse("inventory_reorder"), {"warehouse": b_data["main"].pk})
    assert "Rodamiento de B" not in reorder.content.decode()


@pytest.mark.parametrize(
    "name,params",
    [
        ("inventory_export_xlsx", {}),
        ("inventory_export_xlsx", {"warehouse": "B"}),
        ("inventory_movements_export_xlsx", {}),
        ("inventory_movements_export_xlsx", {"warehouse": "B"}),
        ("inventory_reorder_export_xlsx", {}),
        ("inventory_reorder_export_xlsx", {"warehouse": "B"}),
    ],
)
def test_excel_files_contain_only_the_own_organization(client, tenant_a, a_data, b_data, name, params):
    login(client, tenant_a.admin)
    params = {key: b_data["main"].pk for key in params}
    text = str(xlsx_rows(client.get(reverse(name), params)))
    assert "Rodamiento de A" in text
    assert "Rodamiento de B" not in text and "DOC-DE-B" not in text and "Norte de B" not in text


# --- Alcance por sitio ----------------------------------------------------------


@pytest.fixture
def scoped(tenant_a):
    with tenant_context(tenant_a.org):
        a = tenant_a.admin
        north = Location.objects.create(code="N", name="Norte")
        south = Location.objects.create(code="S", name="Sur")
        services.ensure_default_warehouse()
        wh_north = services.create_warehouse(membership=a, code="NOR", name="Bodega norte", location=north)
        wh_south = services.create_warehouse(membership=a, code="SUR", name="Bodega sur", location=south)
        part = services.create_part(membership=a, code="ROD", name="Rodamiento")
        services.receive_stock(membership=a, part=part, quantity=3, warehouse=wh_south, reference="DOC-SUR")
        services.set_stock_levels(membership=a, part=part, warehouse=wh_south, min_quantity=9)
        a.restricted_to_locations = True
        a.save()
        MembershipLocation.objects.create(membership=a, location=north)
    return {"north": wh_north, "south": wh_south, "part": part, "membership": a}


def test_restricted_membership_does_not_see_or_move_stock_out_of_scope(client, scoped):
    login(client, scoped["membership"])
    receive = client.get(reverse("inventory_receive")).content.decode()
    assert "Bodega norte" in receive and "Bodega sur" not in receive
    response = client.post(
        reverse("inventory_receive"),
        {
            "part": scoped["part"].pk,
            "quantity": "1",
            "warehouse": scoped["south"].pk,
            "client_uuid": uuid.uuid4(),
        },
    )
    assert response.context["form"].errors["warehouse"]
    movements = client.get(reverse("inventory_movements")).content.decode()
    assert "DOC-SUR" not in movements
    reorder = client.get(reverse("inventory_reorder"))
    assert reorder.context["page"].paginator.count == 0
    rows = xlsx_rows(client.get(reverse("inventory_movements_export_xlsx")))
    assert len(rows) == 1
    with tenant_context(scoped["part"].organization):
        assert Warehouse.objects.get(pk=scoped["south"].pk).movements.count() == 1
