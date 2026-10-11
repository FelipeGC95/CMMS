"""Aislamiento de las pantallas de inventario (INV-16 CA3): IDs de otra organización en URLs y
formularios, y alcance por sitio de las bodegas (INV-03 CA5)."""

import pytest
from django.urls import reverse

from apps.accounts.models import MembershipLocation
from apps.core.tenancy import tenant_context
from apps.inventory import services
from apps.inventory.models import Part, StockItem, Warehouse
from apps.organizations.models import Location
from tests.conftest import login
from tests.test_inventory_import import row, xlsx

pytestmark = pytest.mark.usefixtures("reconciled_inventory")


@pytest.fixture
def b_data(tenant_b):
    with tenant_context(tenant_b.org):
        part = services.create_part(membership=tenant_b.admin, code="ROD", name="Rodamiento de B")
        main = services.ensure_default_warehouse()
        other = services.create_warehouse(membership=tenant_b.admin, code="BNORTE", name="Norte de B")
        site = Location.objects.create(code="SB", name="Sitio de B")
        services.receive_stock(membership=tenant_b.admin, part=part, quantity=5, unit_cost="10")
    return {"part": part, "main": main, "other": other, "site": site}


@pytest.fixture
def a_part(tenant_a):
    with tenant_context(tenant_a.org):
        part = services.create_part(membership=tenant_a.admin, code="ROD", name="Rodamiento de A")
        services.ensure_default_warehouse()
    return part


def test_lists_and_search_do_not_show_another_organization(client, tenant_a, b_data, a_part):
    login(client, tenant_a.admin)
    home = client.get(reverse("inventory_home")).content.decode()
    assert "Rodamiento de A" in home and "Rodamiento de B" not in home
    search = client.get(reverse("inventory_part_search"), {"q": "rod"}).content.decode()
    assert "Rodamiento de A" in search and "Rodamiento de B" not in search
    warehouses = client.get(reverse("inventory_warehouse_list")).content.decode()
    assert "Norte de B" not in warehouses
    assert client.get(reverse("inventory_home"), {"warehouse": b_data["other"].pk}).status_code == 200


@pytest.mark.parametrize(
    "name,method",
    [
        ("inventory_part_detail", "get"),
        ("inventory_part_edit", "get"),
        ("inventory_part_edit", "post"),
        ("inventory_part_active", "post"),
        ("inventory_part_levels", "post"),
    ],
)
def test_part_urls_with_another_organization_id_are_404(client, tenant_a, b_data, a_part, name, method):
    login(client, tenant_a.admin)
    url = reverse(name, args=[b_data["part"].pk])
    data = {"code": "X", "name": "X", "is_active": "0", "min_quantity": "1", "warehouse": b_data["main"].pk}
    response = getattr(client, method)(url, data) if method == "post" else client.get(url)
    assert response.status_code == 404
    with tenant_context(tenant_b_org := b_data["part"].organization):
        part = Part.objects.get(pk=b_data["part"].pk)
        assert part.is_active and part.code == "ROD"
        assert not StockItem.objects.filter(min_quantity__gt=0).exists()
    assert tenant_b_org.pk != tenant_a.org.pk


@pytest.mark.parametrize(
    "name,method",
    [
        ("inventory_warehouse_edit", "get"),
        ("inventory_warehouse_edit", "post"),
        ("inventory_warehouse_default", "post"),
        ("inventory_warehouse_active", "post"),
    ],
)
def test_warehouse_urls_with_another_organization_id_are_404(client, tenant_a, b_data, name, method):
    login(client, tenant_a.admin)
    url = reverse(name, args=[b_data["other"].pk])
    response = getattr(client, method)(url, {"code": "X", "name": "X", "is_active": "0"})
    assert response.status_code == 404
    with tenant_context(b_data["other"].organization):
        other = Warehouse.objects.get(pk=b_data["other"].pk)
        assert other.is_active and not other.is_default and other.name == "Norte de B"


def test_levels_with_a_warehouse_of_another_organization_is_404(client, tenant_a, b_data, a_part):
    login(client, tenant_a.admin)
    response = client.post(
        reverse("inventory_part_levels", args=[a_part.pk]),
        {"warehouse": b_data["main"].pk, "min_quantity": "3"},
    )
    assert response.status_code == 404
    with tenant_context(tenant_a.org):
        assert not StockItem.objects.exists()


def test_warehouse_form_rejects_a_location_of_another_organization(client, tenant_a, b_data):
    login(client, tenant_a.admin)
    response = client.post(
        reverse("inventory_warehouse_create"), {"code": "X", "name": "X", "location": b_data["site"].pk}
    )
    assert response.status_code == 200
    assert response.context["form"].errors["location"]
    with tenant_context(tenant_a.org):
        assert not Warehouse.objects.filter(code="X").exists()


def test_import_cannot_target_a_warehouse_of_another_organization(client, tenant_a, b_data):
    login(client, tenant_a.admin)
    response = client.post(
        reverse("inventory_import"),
        {"file": xlsx([row("ROD", "Rodamiento", warehouse="BNORTE", quantity=1)])},
    )
    assert response.context["token"] == ""
    assert "No existe la bodega «BNORTE»." in response.content.decode()


def test_import_updates_only_parts_of_its_own_organization(client, tenant_a, b_data):
    login(client, tenant_a.admin)
    response = client.post(reverse("inventory_import"), {"file": xlsx([row("ROD", "Mío", quantity=2)])})
    assert [r.action for r in response.context["preview"].rows] == ["create"]  # el ROD de B no cuenta
    client.post(reverse("inventory_import_confirm", args=[response.context["token"]]))
    with tenant_context(b_data["part"].organization):
        assert Part.objects.get(code="ROD").name == "Rodamiento de B"
        assert StockItem.objects.get().on_hand == 5


# --- Alcance por sitio ----------------------------------------------------------


@pytest.fixture
def scoped(tenant_a):
    with tenant_context(tenant_a.org):
        north = Location.objects.create(code="N", name="Norte")
        south = Location.objects.create(code="S", name="Sur")
        services.ensure_default_warehouse()
        wh_north = services.create_warehouse(
            membership=tenant_a.admin, code="NOR", name="Bodega norte", location=north
        )
        wh_south = services.create_warehouse(
            membership=tenant_a.admin, code="SUR", name="Bodega sur", location=south
        )
        part = services.create_part(membership=tenant_a.admin, code="ROD", name="Rodamiento")
        services.receive_stock(membership=tenant_a.admin, part=part, quantity=3, warehouse=wh_south)
        planner = tenant_a.admin
        planner.restricted_to_locations = True
        planner.save()
        MembershipLocation.objects.create(membership=planner, location=north)
    return {"north": wh_north, "south": wh_south, "part": part, "membership": planner, "south_site": south}


def test_restricted_membership_only_sees_warehouses_in_scope(client, scoped):
    login(client, scoped["membership"])
    page = client.get(reverse("inventory_warehouse_list")).content.decode()
    assert "Bodega norte" in page and "Bodega sur" not in page
    assert client.get(reverse("inventory_warehouse_edit", args=[scoped["south"].pk])).status_code == 404
    detail = client.get(reverse("inventory_part_detail", args=[scoped["part"].pk])).content.decode()
    assert "Bodega sur" not in detail and "Bodega norte" in detail
    home = client.get(reverse("inventory_home"))
    assert [p.on_hand_total for p in home.context["page"].object_list] == [0]


def test_restricted_membership_cannot_set_levels_or_pick_locations_out_of_scope(client, scoped):
    login(client, scoped["membership"])
    response = client.post(
        reverse("inventory_part_levels", args=[scoped["part"].pk]),
        {"warehouse": scoped["south"].pk, "min_quantity": "5"},
    )
    assert response.status_code == 404
    response = client.post(
        reverse("inventory_warehouse_create"), {"code": "X", "name": "X", "location": scoped["south_site"].pk}
    )
    assert response.context["form"].errors["location"]
