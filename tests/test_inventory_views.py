"""Pantallas web de inventario (INV-01, INV-03, INV-04, INV-12): catálogo, ficha, mínimos y bodegas."""

from decimal import Decimal

import pytest
from django.urls import reverse

from apps.audit.models import AuditEvent
from apps.core.tenancy import tenant_context
from apps.inventory import services
from apps.inventory.models import Part, StockItem, Warehouse
from apps.inventory.selectors import part_search, stock_counters
from tests.conftest import login

pytestmark = pytest.mark.usefixtures("reconciled_inventory")


@pytest.fixture
def stock(tenant_a):
    """Tres repuestos: uno con stock y costo, uno bajo mínimo y uno en negativo."""
    with tenant_context(tenant_a.org):
        a = tenant_a.admin
        ok = services.create_part(membership=a, code="ROD-1", name="Rodamiento 6205", category="Rodamientos")
        low = services.create_part(membership=a, code="FIL-1", name="Filtro de aire", category="Filtros")
        neg = services.create_part(membership=a, code="COR-1", name="Correa B42", manufacturer_ref="B42-X")
        services.receive_stock(membership=a, part=ok, quantity=10, unit_cost="1500")
        services.receive_stock(membership=a, part=low, quantity=1)  # sin costo
        services.set_stock_levels(membership=a, part=low, min_quantity=3)
        services.count_stock(membership=a, part=neg, counted_quantity=0, note="inicio")
        from tests.inventory_utils import make_work_order

        services.issue_to_work_order(
            work_order=make_work_order(tenant_a), part=neg, quantity=2, by=tenant_a.tech.user
        )
    return {"ok": ok, "low": low, "neg": neg}


# --- Inicio y stock -------------------------------------------------------------


def test_empty_state_offers_import_and_add_and_plans_card_only_with_plan_parts(client, tenant_a):
    login(client, tenant_a.admin)
    body = client.get(reverse("inventory_home")).content.decode()
    assert reverse("inventory_import") in body
    assert reverse("inventory_part_create") in body
    assert reverse("inventory_from_plans") not in body  # sin repuestos en los planes: nada que crear
    with tenant_context(tenant_a.org):
        from apps.maintenance import services as plans

        plans.create_plan(
            code="PMR",
            name="Con repuestos",
            calendar_interval=30,
            items=[{"text": "Revisar", "item_type": "check"}],
            resources=[
                {"kind": "part", "description": "Filtro de aceite", "quantity": 1, "unit": "un"},
                {"kind": "part", "description": "Correa B42", "quantity": 2, "unit": "un"},
            ],
        )
    body = client.get(reverse("inventory_home")).content.decode()
    assert reverse("inventory_from_plans") in body
    assert "Crear desde los repuestos de tus planes" in body and "(2)" in body


def test_empty_state_for_viewers_has_no_management_actions(client, tenant_a):
    login(client, tenant_a.viewer)
    body = client.get(reverse("inventory_home")).content.decode()
    assert "Aún no hay repuestos en el catálogo." in body
    assert reverse("inventory_import") not in body


def test_home_lists_stock_with_marks_and_counters(client, tenant_a, stock):
    login(client, tenant_a.admin)
    response = client.get(reverse("inventory_home"))
    body = response.content.decode()
    assert response.status_code == 200
    for part in stock.values():
        assert reverse("inventory_part_detail", args=[part.pk]) in body
    assert "Negativo · regularizar" in body and "Bajo mínimo" in body and "Sin costo" in body
    assert response.context["counters"] == {"below_min": 1, "negative": 1, "no_cost": 1}
    assert 'data-label="Disponible"' in body


def test_home_filters_by_status_search_and_category(client, tenant_a, stock):
    login(client, tenant_a.admin)

    def codes(**params):
        response = client.get(reverse("inventory_home"), params)
        return [part.code for part in response.context["page"].object_list]

    assert codes(status="below_min") == ["FIL-1"]
    assert codes(status="negative") == ["COR-1"]
    assert codes(status="no_cost") == ["FIL-1"]
    assert codes(q="b42-x") == ["COR-1"]  # n° de parte, sin mayúsculas
    assert codes(category="filtros") == ["FIL-1"]
    assert codes(status="bogus") == ["COR-1", "FIL-1", "ROD-1"]


def test_technicians_do_not_see_costs_on_home_or_detail(client, tenant_a, stock):
    login(client, tenant_a.tech)
    home = client.get(reverse("inventory_home")).content.decode()
    assert "Costo promedio" not in home and "Sin costo" not in home
    detail = client.get(reverse("inventory_part_detail", args=[stock["ok"].pk])).content.decode()
    assert "Costo promedio" not in detail and "1.500" not in detail
    assert reverse("inventory_part_edit", args=[stock["ok"].pk]) not in detail


def test_warehouse_filter_only_with_more_than_one_active_warehouse(client, tenant_a, stock):
    login(client, tenant_a.admin)
    assert 'name="warehouse"' not in client.get(reverse("inventory_home")).content.decode()
    with tenant_context(tenant_a.org):
        other = services.create_warehouse(membership=tenant_a.admin, code="B2", name="Bodega norte")
        services.transfer_stock(
            membership=tenant_a.admin,
            part=stock["ok"],
            from_warehouse=services.ensure_default_warehouse(),
            to_warehouse=other,
            quantity=4,
        )
    response = client.get(reverse("inventory_home"), {"warehouse": other.pk})
    assert 'name="warehouse"' in response.content.decode()
    assert [p.on_hand_total for p in response.context["page"].object_list if p.code == "ROD-1"] == [4]


def test_stock_counters_and_search_selectors(in_a, stock):
    assert stock_counters(membership=in_a.admin) == {"below_min": 1, "negative": 1, "no_cost": 1}
    assert [p.code for p in part_search(membership=in_a.admin, q="rod")] == ["ROD-1"]
    assert part_search(membership=in_a.admin, q="  ") == []
    services.set_part_active(membership=in_a.admin, part=stock["ok"], is_active=False)
    assert part_search(membership=in_a.admin, q="rod") == []  # desactivado: fuera del buscador


# --- Buscador htmx --------------------------------------------------------------


def test_part_search_returns_links_or_radio_options(client, tenant_a, stock):
    login(client, tenant_a.tech)
    url = reverse("inventory_part_search")
    links = client.get(url, {"q": "filtro"}).content.decode()
    assert reverse("inventory_part_detail", args=[stock["low"].pk]) in links
    picks = client.get(url, {"q": "filtro", "pick": "part"}).content.decode()
    assert f'name="part" value="{stock["low"].pk}"' in picks
    assert 'type="radio"' in picks
    unsafe = client.get(url, {"q": "filtro", "pick": '"><script>'}).content.decode()
    assert "<script>" not in unsafe and 'type="radio"' not in unsafe
    assert "No hay repuestos activos" in client.get(url, {"q": "zzz"}).content.decode()


# --- Alta, edición y estado del repuesto ---------------------------------------


def test_create_part_through_the_form(client, tenant_a):
    login(client, tenant_a.admin)
    response = client.post(
        reverse("inventory_part_create"),
        {"code": "VAL-1", "name": "Válvula", "unit": "", "category": "Válvulas"},
    )
    with tenant_context(tenant_a.org):
        part = Part.objects.get(code="VAL-1")
    assert response.status_code == 302
    assert response.url == reverse("inventory_part_detail", args=[part.pk])
    assert part.unit == "un" and part.created_by == tenant_a.admin.user


def test_duplicate_code_error_is_shown_on_the_field(client, tenant_a, stock):
    login(client, tenant_a.admin)
    response = client.post(reverse("inventory_part_create"), {"code": "rod-1", "name": "Otro"})
    assert response.status_code == 200
    assert response.context["form"].errors["code"]
    assert "Ya existe otro con el código rod-1." in response.content.decode()


def test_edit_part_and_category_suggestions(client, tenant_a, stock):
    login(client, tenant_a.admin)
    url = reverse("inventory_part_edit", args=[stock["ok"].pk])
    page = client.get(url).content.decode()
    assert 'list="part-categories"' in page and '<option value="Filtros">' in page
    response = client.post(url, {"code": "ROD-1", "name": "Rodamiento 6205 2RS", "unit": "un"})
    assert response.status_code == 302
    with tenant_context(tenant_a.org):
        stock["ok"].refresh_from_db()
    assert stock["ok"].name == "Rodamiento 6205 2RS"


def test_deactivate_and_reactivate_part(client, tenant_a, stock):
    login(client, tenant_a.admin)
    url = reverse("inventory_part_active", args=[stock["ok"].pk])
    client.post(url, {"is_active": "0"})
    with tenant_context(tenant_a.org):
        stock["ok"].refresh_from_db()
    assert not stock["ok"].is_active
    detail = client.get(reverse("inventory_part_detail", args=[stock["ok"].pk])).content.decode()
    assert "Repuesto desactivado" in detail and "Reactivar" in detail
    assert "Entrada" in detail  # el historial sigue visible
    client.post(url, {"is_active": "1"})
    with tenant_context(tenant_a.org):
        stock["ok"].refresh_from_db()
    assert stock["ok"].is_active


def test_active_toggle_requires_post(client, tenant_a, stock):
    login(client, tenant_a.admin)
    assert client.get(reverse("inventory_part_active", args=[stock["ok"].pk])).status_code == 405


# --- Ficha y mínimos ------------------------------------------------------------


def test_part_detail_shows_stock_by_warehouse_costs_and_movements(client, tenant_a, stock):
    login(client, tenant_a.admin)
    response = client.get(reverse("inventory_part_detail", args=[stock["ok"].pk]))
    body = response.content.decode()
    assert response.status_code == 200
    assert "Costo promedio" in body and "$ 1.500" in body
    assert "Entrada" in body and "+10" in body
    assert 'name="min_quantity"' in body and 'name="warehouse"' in body
    # Una sola bodega activa: sin columna ni selector de bodega (INV-03 CA4).
    assert "<th>Bodega</th>" not in body and "<select" not in body


def test_part_detail_of_unknown_part_is_404(client, tenant_a):
    login(client, tenant_a.admin)
    assert client.get(reverse("inventory_part_detail", args=[Warehouse().pk])).status_code == 404


def test_set_levels_with_htmx_returns_the_updated_stock_section(client, tenant_a, stock):
    login(client, tenant_a.admin)
    with tenant_context(tenant_a.org):
        warehouse = services.ensure_default_warehouse()
    response = client.post(
        reverse("inventory_part_levels", args=[stock["ok"].pk]),
        {"warehouse": warehouse.pk, "min_quantity": "12", "target_quantity": "20", "bin_location": "A-3"},
        HTTP_HX_REQUEST="true",
    )
    body = response.content.decode()
    assert response.status_code == 200
    assert 'id="stock-section"' in body and "Guardado" in body and "Bajo mínimo" in body
    with tenant_context(tenant_a.org):
        item = StockItem.objects.get(part=stock["ok"], warehouse=warehouse)
    assert (item.min_quantity, item.target_quantity, item.bin_location) == (Decimal(12), Decimal(20), "A-3")
    with tenant_context(tenant_a.org):
        assert AuditEvent.objects.filter(
            action=AuditEvent.Action.UPDATE, target_id=str(stock["ok"].pk)
        ).exists()


def test_set_levels_rejects_target_below_minimum(client, tenant_a, stock):
    login(client, tenant_a.admin)
    with tenant_context(tenant_a.org):
        warehouse = services.ensure_default_warehouse()
    url = reverse("inventory_part_levels", args=[stock["ok"].pk])
    data = {"warehouse": warehouse.pk, "min_quantity": "5", "target_quantity": "2"}
    body = client.post(url, data, HTTP_HX_REQUEST="true").content.decode()
    assert "El objetivo no puede ser menor que el mínimo." in body
    response = client.post(url, data)  # sin htmx: vuelve a la ficha con el mensaje
    assert response.status_code == 302
    with tenant_context(tenant_a.org):
        assert not StockItem.objects.get(part=stock["ok"]).min_quantity


def test_set_levels_creates_the_default_warehouse_row_without_movements(client, tenant_a):
    """Un repuesto recién creado ya puede tener mínimo (la bodega por defecto se crea sola)."""
    login(client, tenant_a.admin)
    with tenant_context(tenant_a.org):
        part = services.create_part(membership=tenant_a.admin, code="N1", name="Nuevo")
        warehouse = services.ensure_default_warehouse()
    page = client.get(reverse("inventory_part_detail", args=[part.pk])).content.decode()
    assert f'value="{warehouse.pk}"' in page
    client.post(
        reverse("inventory_part_levels", args=[part.pk]), {"warehouse": warehouse.pk, "min_quantity": "2"}
    )
    with tenant_context(tenant_a.org):
        assert StockItem.objects.get(part=part).min_quantity == 2


# --- Bodegas --------------------------------------------------------------------


def test_create_edit_and_default_warehouse(client, tenant_a, stock):
    login(client, tenant_a.admin)
    with tenant_context(tenant_a.org):
        site = tenant_a.asset.location
    response = client.post(
        reverse("inventory_warehouse_create"),
        {"code": "NOR", "name": "Bodega norte", "location": site.pk if site else ""},
    )
    assert response.status_code == 302
    with tenant_context(tenant_a.org):
        north = Warehouse.objects.get(code="NOR")
    client.post(reverse("inventory_warehouse_edit", args=[north.pk]), {"code": "NOR", "name": "Norte"})
    client.post(reverse("inventory_warehouse_default", args=[north.pk]))
    with tenant_context(tenant_a.org):
        north.refresh_from_db()
    assert north.name == "Norte" and north.is_default
    page = client.get(reverse("inventory_warehouse_list")).content.decode()
    assert "Por defecto" in page and "Marcar por defecto" in page


def test_duplicate_warehouse_code_is_a_field_error(client, tenant_a, stock):
    login(client, tenant_a.admin)
    response = client.post(reverse("inventory_warehouse_create"), {"code": "bod", "name": "Otra"})
    assert response.status_code == 200
    assert response.context["form"].errors["code"]


def test_warehouse_deactivation_explains_why_not(client, tenant_a, stock):
    login(client, tenant_a.admin)
    with tenant_context(tenant_a.org):
        main = services.ensure_default_warehouse()
        empty = services.create_warehouse(membership=tenant_a.admin, code="VAC", name="Vacía")
        full = services.create_warehouse(membership=tenant_a.admin, code="LLE", name="Llena")
        services.receive_stock(membership=tenant_a.admin, part=stock["ok"], quantity=1, warehouse=full)
    url = lambda w: reverse("inventory_warehouse_active", args=[w.pk])  # noqa: E731
    page = client.post(url(main), {"is_active": "0"}, follow=True).content.decode()
    assert "La bodega por defecto no se puede desactivar" in page
    page = client.post(url(full), {"is_active": "0"}, follow=True).content.decode()
    assert "La bodega tiene stock" in page
    page = client.post(url(empty), {"is_active": "0"}, follow=True).content.decode()
    assert "Bodega VAC desactivada." in page and "Reactivar" in page
    client.post(url(empty), {"is_active": "1"})
    with tenant_context(tenant_a.org):
        empty.refresh_from_db()
    assert empty.is_active


def test_warehouse_list_paginates_and_shows_empty_state(client, tenant_a):
    login(client, tenant_a.viewer)
    page = client.get(reverse("inventory_warehouse_list")).content.decode()
    assert "Bodega principal" in page  # el estado vacío explica que se crea sola
    assert reverse("inventory_warehouse_create") not in page


# --- Navegación -----------------------------------------------------------------


@pytest.mark.parametrize("role", ["admin", "tech", "viewer"])
def test_inventory_link_in_desktop_nav_and_more_menu(client, tenant_a, role):
    login(client, getattr(tenant_a, role))
    url = reverse("inventory_home")
    assert f'href="{url}"' in client.get(reverse("dashboard")).content.decode()
    assert f'href="{url}"' in client.get(reverse("menu")).content.decode()


def test_inventory_link_hidden_without_module_or_permission(client, tenant_a):
    from apps.organizations.models import Plan
    from tests.inventory_utils import add_member

    url = f'href="{reverse("inventory_home")}"'
    login(client, add_member(tenant_a, "external"))
    assert url not in client.get(reverse("dashboard")).content.decode()
    assert url not in client.get(reverse("menu")).content.decode()
    tenant_a.org.plan = Plan.objects.get(code="basic")
    tenant_a.org.save(update_fields=["plan"])
    login(client, tenant_a.admin)
    assert url not in client.get(reverse("dashboard")).content.decode()
    assert url not in client.get(reverse("menu")).content.decode()
