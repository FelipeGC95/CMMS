"""Pantallas de movimientos de inventario (INV-05, INV-06, INV-07, INV-12, INV-14): entrada,
conteo, transferencia, libro de movimientos, reposición y Excel. Los servicios ya están
probados en tests/test_inventory_services.py; aquí, vistas y formularios."""

import datetime
import uuid
import zoneinfo
from decimal import Decimal

import pytest
from django.contrib.messages import get_messages
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Membership
from apps.core.tenancy import tenant_context
from apps.inventory import services
from apps.inventory.models import Part, StockItem, StockMovement, Warehouse
from apps.inventory.selectors import reorder_list
from apps.notifications.models import Kind, Notification
from apps.organizations.models import Plan
from tests.conftest import login
from tests.inventory_utils import add_member, make_work_order
from tests.test_exports_excel import xlsx_rows

pytestmark = pytest.mark.usefixtures("reconciled_inventory")
MovementKind = StockMovement.Kind


@pytest.fixture
def part(tenant_a):
    with tenant_context(tenant_a.org):
        return services.create_part(membership=tenant_a.admin, code="ROD-1", name="Rodamiento 6205")


@pytest.fixture
def second_warehouse(tenant_a):
    with tenant_context(tenant_a.org):
        services.ensure_default_warehouse()
        return services.create_warehouse(membership=tenant_a.admin, code="NORTE", name="Bodega Norte")


def _messages(response):
    return [str(m) for m in get_messages(response.wsgi_request)]


def _movements(tenant, **filters):
    with tenant_context(tenant.org):
        return list(StockMovement.objects.filter(**filters).order_by("created_at"))


def _on_hand(tenant, part, warehouse=None):
    with tenant_context(tenant.org):
        warehouse = warehouse or Warehouse.objects.get(is_default=True)
        item = StockItem.objects.filter(part=part, warehouse=warehouse).first()
        return item.on_hand if item else Decimal(0)


# --- Entrada (INV-05) -----------------------------------------------------------


def test_receive_with_part_preselected_shows_it_and_no_warehouse_with_one(client, tenant_a, part):
    login(client, tenant_a.admin)
    response = client.get(reverse("inventory_receive"), {"part": part.pk})
    body = response.content.decode()
    assert response.status_code == 200
    assert f'name="part" value="{part.pk}"' in body and "Rodamiento 6205" in body
    assert 'name="warehouse"' not in body  # INV-03 CA4: una sola bodega
    assert 'name="client_uuid"' in body


def test_receive_search_without_js_renders_radio_options(client, tenant_a, part):
    login(client, tenant_a.admin)
    body = client.get(reverse("inventory_receive"), {"q": "rod"}).content.decode()
    assert f'type="radio" id="pick-{part.pk}" name="part"' in body


def test_receive_and_register_another_keeps_warehouse_and_document(client, tenant_a, part, second_warehouse):
    login(client, tenant_a.admin)
    response = client.post(
        reverse("inventory_receive"),
        {
            "part": part.pk,
            "quantity": "4",
            "unit_cost": "1500",
            "warehouse": second_warehouse.pk,
            "reference": "GD-77",
            "supplier": "Rodamientos SA",
            "client_uuid": uuid.uuid4(),
            "action": "another",
        },
    )
    assert response.status_code == 302
    assert response.url.startswith(reverse("inventory_receive"))
    assert f"warehouse={second_warehouse.pk}" in response.url and "reference=GD-77" in response.url
    [movement] = _movements(tenant_a)
    assert (movement.kind, movement.quantity, movement.reference) == (
        MovementKind.RECEIPT,
        Decimal(4),
        "GD-77",
    )
    assert movement.warehouse_id == second_warehouse.pk and movement.unit_cost == Decimal(1500)
    follow = client.get(response.url).content.decode()
    assert 'value="GD-77"' in follow
    assert f'<option value="{second_warehouse.pk}" selected>' in follow


def test_receive_save_goes_to_the_part_with_a_past_date(client, tenant_a, part):
    login(client, tenant_a.admin)
    # El formulario recibe la hora local de la organización.
    when = timezone.localtime(timezone=zoneinfo.ZoneInfo(tenant_a.org.timezone)) - datetime.timedelta(days=2)
    response = client.post(
        reverse("inventory_receive"),
        {
            "part": part.pk,
            "quantity": "2",
            "occurred_at": when.strftime("%Y-%m-%dT%H:%M"),
            "client_uuid": uuid.uuid4(),
            "action": "save",
        },
    )
    assert response.url == reverse("inventory_part_detail", args=[part.pk])
    [movement] = _movements(tenant_a)
    assert abs(movement.occurred_at - when) < datetime.timedelta(minutes=1)
    assert _on_hand(tenant_a, part) == 2


def test_receive_double_submit_creates_one_movement(client, tenant_a, part):
    login(client, tenant_a.admin)
    data = {"part": part.pk, "quantity": "3", "client_uuid": uuid.uuid4(), "action": "save"}
    assert client.post(reverse("inventory_receive"), data).status_code == 302
    assert client.post(reverse("inventory_receive"), data).status_code == 302
    assert len(_movements(tenant_a)) == 1
    assert _on_hand(tenant_a, part) == 3


def test_receive_with_zero_cost_warns_that_the_average_drops(client, tenant_a, part):
    login(client, tenant_a.admin)
    data = {"part": part.pk, "quantity": "1", "unit_cost": "0", "client_uuid": uuid.uuid4()}
    response = client.post(reverse("inventory_receive"), data)
    assert any("costo 0" in text for text in _messages(response))


def test_receive_errors_stay_on_the_form(client, tenant_a, part):
    login(client, tenant_a.admin)
    response = client.post(reverse("inventory_receive"), {"quantity": "2", "client_uuid": uuid.uuid4()})
    assert response.status_code == 200 and "Elige un repuesto." in response.content.decode()
    response = client.post(
        reverse("inventory_receive"), {"part": part.pk, "quantity": "0", "client_uuid": uuid.uuid4()}
    )
    assert "La cantidad debe ser mayor que cero." in response.content.decode()
    assert not _movements(tenant_a)


def test_supervisor_receives_with_cost(client, tenant_a, part):
    supervisor = add_member(tenant_a, Membership.Role.SUPERVISOR)
    login(client, supervisor)
    assert 'name="unit_cost"' in client.get(reverse("inventory_receive")).content.decode()
    data = {"part": part.pk, "quantity": "1", "unit_cost": "50", "client_uuid": uuid.uuid4()}
    assert client.post(reverse("inventory_receive"), data).status_code == 302
    assert _movements(tenant_a)[0].unit_cost == 50


# --- Conteo (INV-06) ------------------------------------------------------------


@pytest.fixture
def counted(tenant_a, part):
    with tenant_context(tenant_a.org):
        services.receive_stock(membership=tenant_a.admin, part=part, quantity=10, unit_cost="100")
    return part


def test_count_pick_redirects_to_the_part_count(client, tenant_a, part):
    login(client, tenant_a.admin)
    body = client.get(reverse("inventory_count"), {"q": "rod", "search": "1"}).content.decode()
    assert f'value="{part.pk}"' in body
    response = client.get(reverse("inventory_count"), {"part": part.pk})
    assert response.url == reverse("inventory_part_count", args=[part.pk])


def test_count_without_difference_informs_and_creates_nothing(client, tenant_a, counted):
    login(client, tenant_a.admin)
    url = reverse("inventory_part_count", args=[counted.pk])
    assert "10" in client.get(url).content.decode()
    response = client.post(url, {"counted_quantity": "10", "client_uuid": uuid.uuid4()})
    assert response.url == reverse("inventory_part_detail", args=[counted.pk])
    assert any("Sin diferencia" in text for text in _messages(response))
    assert len(_movements(tenant_a)) == 1


@pytest.mark.parametrize("counted_quantity", ["9", "12"])  # falta una unidad; sobra el 20 %
def test_count_requires_a_reason_when_short_or_over_ten_percent(client, tenant_a, counted, counted_quantity):
    login(client, tenant_a.admin)
    url = reverse("inventory_part_count", args=[counted.pk])
    response = client.post(url, {"counted_quantity": counted_quantity, "client_uuid": uuid.uuid4()})
    assert response.status_code == 200
    assert "Indica el motivo" in response.content.decode()
    response = client.post(
        url, {"counted_quantity": counted_quantity, "note": "Conteo anual", "client_uuid": uuid.uuid4()}
    )
    assert response.status_code == 302
    adjustment = _movements(tenant_a, kind=MovementKind.ADJUSTMENT)[0]
    assert adjustment.on_hand_after == Decimal(counted_quantity) and adjustment.note == "Conteo anual"


def test_count_small_surplus_needs_no_reason_and_double_submit_is_idempotent(client, tenant_a, counted):
    login(client, tenant_a.admin)
    url = reverse("inventory_part_count", args=[counted.pk])
    data = {"counted_quantity": "11", "client_uuid": uuid.uuid4()}
    assert client.post(url, data).status_code == 302
    assert client.post(url, data).status_code == 302
    assert [m.quantity for m in _movements(tenant_a, kind=MovementKind.ADJUSTMENT)] == [Decimal(1)]


def test_count_below_minimum_from_the_web_alerts_once_a_day(
    client, tenant_a, counted, django_capture_on_commit_callbacks
):
    planner = add_member(tenant_a, Membership.Role.PLANNER)
    with tenant_context(tenant_a.org):
        services.set_stock_levels(membership=tenant_a.admin, part=counted, min_quantity=5)
    login(client, tenant_a.admin)
    url = reverse("inventory_part_count", args=[counted.pk])
    with django_capture_on_commit_callbacks(execute=True):
        client.post(url, {"counted_quantity": "4", "note": "Merma", "client_uuid": uuid.uuid4()})
    with django_capture_on_commit_callbacks(execute=True):
        client.post(url, {"counted_quantity": "8", "note": "Apareció", "client_uuid": uuid.uuid4()})
        client.post(url, {"counted_quantity": "3", "note": "Merma", "client_uuid": uuid.uuid4()})
    with tenant_context(tenant_a.org):
        notes = list(Notification.objects.filter(kind=Kind.STOCK_LOW))
    assert [n.recipient_id for n in notes] == [planner.user_id]  # quien cuenta no se avisa a sí mismo
    assert notes[0].url == reverse("inventory_part_detail", args=[counted.pk])


# --- Transferencia (INV-07) -----------------------------------------------------


def test_transfer_needs_two_active_warehouses(client, tenant_a, part):
    login(client, tenant_a.admin)
    response = client.get(reverse("inventory_transfer"))
    assert response.status_code == 302 and response.url == reverse("inventory_warehouse_list")


def test_transfer_creates_the_linked_pair_once(client, tenant_a, counted, second_warehouse):
    login(client, tenant_a.admin)
    with tenant_context(tenant_a.org):
        main = Warehouse.objects.get(is_default=True)
    body = client.get(reverse("inventory_transfer"), {"part": counted.pk}).content.decode()
    assert "Bodega Norte" in body and 'name="from_warehouse"' in body
    data = {
        "part": counted.pk,
        "from_warehouse": main.pk,
        "to_warehouse": second_warehouse.pk,
        "quantity": "4",
        "client_uuid": uuid.uuid4(),
    }
    assert client.post(reverse("inventory_transfer"), data).status_code == 302
    assert client.post(reverse("inventory_transfer"), data).status_code == 302
    pair = _movements(tenant_a, kind__in=[MovementKind.TRANSFER_OUT, MovementKind.TRANSFER_IN])
    assert [m.quantity for m in pair] == [Decimal(-4), Decimal(4)]
    assert _on_hand(tenant_a, counted, main) == 6 and _on_hand(tenant_a, counted, second_warehouse) == 4


def test_transfer_to_the_same_warehouse_is_an_error_on_the_field(client, tenant_a, counted, second_warehouse):
    login(client, tenant_a.admin)
    data = {
        "part": counted.pk,
        "from_warehouse": second_warehouse.pk,
        "to_warehouse": second_warehouse.pk,
        "quantity": "1",
        "client_uuid": uuid.uuid4(),
    }
    response = client.post(reverse("inventory_transfer"), data)
    assert response.status_code == 200
    assert "Elige una bodega de destino distinta" in response.content.decode()


# --- Libro de movimientos (INV-14) ----------------------------------------------


@pytest.fixture
def ledger(tenant_a, part, second_warehouse):
    """Entrada, transferencia, salida a OT (queda negativo en NORTE) y un repuesto aparte."""
    with tenant_context(tenant_a.org):
        a = tenant_a.admin
        other = services.create_part(membership=a, code="FIL-9", name="Filtro de aire")
        services.receive_stock(membership=a, part=part, quantity=5, unit_cost="200", reference="FAC-1")
        services.receive_stock(membership=a, part=other, quantity=1)
        main = Warehouse.objects.get(is_default=True)
        services.transfer_stock(
            membership=a, part=part, from_warehouse=main, to_warehouse=second_warehouse, quantity=2
        )
        wo = make_work_order(tenant_a)
        services.issue_to_work_order(
            work_order=wo, part=part, quantity=3, by=tenant_a.tech.user, warehouse=second_warehouse
        )
    return {"part": part, "other": other, "wo": wo, "main": main, "north": second_warehouse}


def test_movements_list_shows_signed_quantities_balance_and_work_order(client, tenant_a, ledger):
    login(client, tenant_a.admin)
    response = client.get(reverse("inventory_movements"))
    body = response.content.decode()
    assert response.status_code == 200 and response.context["page"].paginator.count == 5
    assert reverse("work_order_detail", args=[ledger["wo"].pk]) in body and ledger["wo"].code in body
    assert "-3" in body and "+5" in body and "Doc. FAC-1" in body
    assert "Costo unitario" in body


@pytest.mark.parametrize(
    "params,expected",
    [
        ({"part": "ROD-1"}, 4),  # código exacto
        ({"part": "filtro"}, 1),  # parte del nombre
        ({"kind": "issue"}, 1),
        ({"kind": "transfer_in"}, 1),
        ({"work_order": "x"}, 0),
    ],
)
def test_movements_filters(client, tenant_a, ledger, params, expected):
    login(client, tenant_a.admin)
    response = client.get(reverse("inventory_movements"), params)
    assert response.context["page"].paginator.count == expected


def test_movements_filter_by_warehouse_dates_and_work_order_code(client, tenant_a, ledger):
    login(client, tenant_a.admin)
    url = reverse("inventory_movements")
    assert client.get(url, {"warehouse": ledger["north"].pk}).context["page"].paginator.count == 2
    assert client.get(url, {"work_order": ledger["wo"].code}).context["page"].paginator.count == 1
    assert client.get(url, {"work_order": str(ledger["wo"].number)}).context["page"].paginator.count == 1
    today = timezone.localdate()
    assert client.get(url, {"date_from": today, "date_to": today}).context["page"].paginator.count == 5
    tomorrow = today + datetime.timedelta(days=1)
    assert client.get(url, {"date_from": tomorrow}).context["page"].paginator.count == 0
    response = client.get(url, {"date_from": tomorrow, "date_to": today})
    assert "La fecha final no puede ser anterior" in response.content.decode()


def test_movements_hide_costs_without_cost_view(client, tenant_a, ledger):
    login(client, tenant_a.tech)
    body = client.get(reverse("inventory_movements")).content.decode()
    assert "Costo unitario" not in body


# --- Reposición (INV-12 CA4) ----------------------------------------------------


@pytest.fixture
def reorder(tenant_a, second_warehouse):
    """En la bodega principal: negativo, reserva sin cubrir, bajo mínimo, con stock y un inactivo."""
    with tenant_context(tenant_a.org):
        a = tenant_a.admin

        def make(code, **levels):
            p = services.create_part(membership=a, code=code, name=f"Repuesto {code}")
            if levels:
                services.set_stock_levels(membership=a, part=p, **levels)
            return p

        wo = make_work_order(tenant_a)
        negative = make("NEG", min_quantity=2, target_quantity=5)
        services.issue_to_work_order(work_order=wo, part=negative, quantity=2, by=a.user)  # -2
        uncovered = make("RES")
        services.receive_stock(membership=a, part=uncovered, quantity=1)
        services.reserve_for_work_order(work_order=wo, part=uncovered, quantity=3, source="manual")
        low = make("LOW", min_quantity="2.5")
        services.receive_stock(membership=a, part=low, quantity=1)
        fine = make("OK", min_quantity=1)
        services.receive_stock(membership=a, part=fine, quantity=5)
        gone = make("OFF", min_quantity=5)
        services.set_part_active(membership=a, part=gone, is_active=False)
        north_low = services.create_part(membership=a, code="NOR", name="Repuesto norte")
        services.set_stock_levels(membership=a, part=north_low, warehouse=second_warehouse, min_quantity=1)
    return {"negative": negative, "uncovered": uncovered, "low": low, "north": north_low}


def test_reorder_list_order_and_suggested_quantities(tenant_a, reorder):
    with tenant_context(tenant_a.org):
        rows = list(reorder_list(membership=tenant_a.admin))
    assert [(r.part.code, r.priority) for r in rows] == [("NEG", 0), ("RES", 1), ("LOW", 2), ("NOR", 2)]
    by_code = {r.part.code: r for r in rows}
    assert by_code["NEG"].suggested == 7  # objetivo 5 − disponible −2
    assert by_code["RES"].suggested == 2 and by_code["RES"].reservation_gap == 2  # 0 − (1 − 3)
    assert by_code["LOW"].suggested == 2  # 2,5 − 1 = 1,5 → 2 (hacia arriba)
    assert by_code["NOR"].suggested == 1


def test_reorder_screen_groups_by_warehouse_with_counter(client, tenant_a, reorder, second_warehouse):
    login(client, tenant_a.admin)
    response = client.get(reverse("inventory_reorder"))
    body = response.content.decode()
    assert response.status_code == 200
    assert "Bodega Norte" in body and "Negativo · regularizar" in body and "Reservas sin cubrir" in body
    assert response.context["counters"]["below_min"] >= 2
    assert reverse("inventory_reorder_export_xlsx") in body
    only_north = client.get(reverse("inventory_reorder"), {"warehouse": second_warehouse.pk})
    assert only_north.context["page"].paginator.count == 1


def test_reorder_empty_state(client, tenant_a, part):
    login(client, tenant_a.admin)
    assert "Nada que reponer" in client.get(reverse("inventory_reorder")).content.decode()


# --- Excel (INV-14 CA5) ---------------------------------------------------------


def test_stock_excel_follows_the_home_filters_and_cost_permission(client, tenant_a, ledger):
    login(client, tenant_a.admin)
    rows = xlsx_rows(client.get(reverse("inventory_export_xlsx")))
    assert "Costo promedio" in rows[0] and len(rows) == 3
    rows = xlsx_rows(client.get(reverse("inventory_export_xlsx"), {"q": "filtro"}))
    assert [r[0] for r in rows[1:]] == ["FIL-9"]
    login(client, tenant_a.tech)
    rows = xlsx_rows(client.get(reverse("inventory_export_xlsx")))
    assert "Costo promedio" not in rows[0]


def test_movements_excel_applies_the_filters(client, tenant_a, ledger):
    login(client, tenant_a.admin)
    rows = xlsx_rows(client.get(reverse("inventory_movements_export_xlsx"), {"kind": "issue"}))
    assert len(rows) == 2 and rows[1][1] == "ROD-1" and rows[1][6] == -3
    assert ledger["wo"].code in rows[1]
    login(client, tenant_a.viewer)
    assert "Costo unitario" in xlsx_rows(client.get(reverse("inventory_movements_export_xlsx")))[0]
    login(client, tenant_a.tech)
    assert "Costo unitario" not in xlsx_rows(client.get(reverse("inventory_movements_export_xlsx")))[0]


def test_reorder_excel_lists_the_suggested_quantities(client, tenant_a, reorder):
    login(client, tenant_a.admin)
    rows = xlsx_rows(client.get(reverse("inventory_reorder_export_xlsx")))
    header = rows[0]
    assert [r[header.index("Código")] for r in rows[1:]] == ["NEG", "RES", "LOW", "NOR"]
    assert rows[1][header.index("Sugerido")] == 7 and "Costo estimado" in header


def test_trial_plan_has_inventory_but_no_excel(client, tenant_a, ledger):
    tenant_a.org.plan = Plan.objects.get(code="trial")
    tenant_a.org.save(update_fields=["plan"])
    login(client, tenant_a.admin)
    for name in ("inventory_home", "inventory_movements", "inventory_reorder"):
        response = client.get(reverse(name))
        assert response.status_code == 200
        assert ".xlsx" not in response.content.decode(), name
    for name in ("inventory_export_xlsx", "inventory_movements_export_xlsx", "inventory_reorder_export_xlsx"):
        assert client.get(reverse(name)).status_code == 403, name


def test_excel_needs_the_inventory_module_too(client, tenant_a, ledger):
    plan = Plan.objects.get(code="pro")
    plan.features = [f for f in plan.features if f != "inventory"]
    plan.save(update_fields=["features"])
    tenant_a.org.plan = plan
    tenant_a.org.save(update_fields=["plan"])
    login(client, tenant_a.admin)
    for name in ("inventory_export_xlsx", "inventory_movements_export_xlsx", "inventory_reorder_export_xlsx"):
        assert client.get(reverse(name)).status_code == 403, name


def test_excel_over_the_row_limit_returns_to_the_list(client, tenant_a, ledger, monkeypatch):
    monkeypatch.setattr("apps.inventory.movement_views.MAX_EXPORT_ROWS", 1)
    login(client, tenant_a.admin)
    response = client.get(reverse("inventory_movements_export_xlsx"), {"kind": "receipt"})
    assert response.status_code == 302
    assert response.url == reverse("inventory_movements") + "?kind=receipt"


def test_part_detail_links_to_its_movements_and_transfer(client, tenant_a, ledger):
    login(client, tenant_a.admin)
    body = client.get(reverse("inventory_part_detail", args=[ledger["part"].pk])).content.decode()
    assert reverse("inventory_movements") + "?part=ROD-1" in body
    assert reverse("inventory_transfer") + f"?part={ledger['part'].pk}" in body
    assert reverse("inventory_part_count", args=[ledger["part"].pk]) in body
    home = client.get(reverse("inventory_home")).content.decode()
    for name in ("inventory_receive", "inventory_count", "inventory_transfer", "inventory_reorder"):
        assert reverse(name) in home, name


def test_inactive_part_is_not_offered_for_receipt(client, tenant_a, part):
    with tenant_context(tenant_a.org):
        services.set_part_active(membership=tenant_a.admin, part=part, is_active=False)
        assert not Part.objects.get(pk=part.pk).is_active
    login(client, tenant_a.admin)
    response = client.post(
        reverse("inventory_receive"), {"part": part.pk, "quantity": "1", "client_uuid": uuid.uuid4()}
    )
    assert "Repuesto no encontrado." in response.content.decode()


def test_design_system_shows_the_movement_components(client, tenant_a):
    login(client, tenant_a.admin)
    html = client.get(reverse("design_system")).content.decode()
    for text in ("picked-part__change", 'class="qty"', "Reservas sin cubrir", "Negativo · regularizar"):
        assert text in html, text
