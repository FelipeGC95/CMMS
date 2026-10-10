"""Servicios de inventario (INV-03 a INV-14) con base real y valores conocidos."""

import random
import uuid
from decimal import Decimal

import pytest
from django.core.management import call_command

from apps.audit.models import AuditEvent
from apps.inventory import services
from apps.inventory.models import ImmutableMovementError, Reservation, StockItem, StockMovement, Warehouse
from apps.inventory.selectors import part_detail, stock_list, visible_warehouses, warehouse_for
from apps.inventory.services import InventoryError
from apps.notifications.models import Kind, Notification
from apps.organizations.models import Location
from apps.work.models import WorkOrder
from tests.inventory_utils import make_work_order, set_status

pytestmark = pytest.mark.usefixtures("reconciled_inventory")
D = Decimal


@pytest.fixture
def part(in_a):
    return services.create_part(membership=in_a.admin, code="ROD-6205", name="Rodamiento 6205")


@pytest.fixture
def wo(in_a):
    return make_work_order(in_a)


def receive(in_a, part, quantity, cost=None, **kwargs):
    return services.receive_stock(
        membership=in_a.admin, part=part, quantity=quantity, unit_cost=cost, **kwargs
    )


def issue(in_a, wo, part, quantity, **kwargs):
    return services.issue_to_work_order(
        work_order=wo, part=part, quantity=quantity, by=in_a.tech.user, **kwargs
    )


def avg(part):
    part.refresh_from_db()
    return part.avg_cost


def on_hand(part, warehouse=None):
    warehouse = warehouse or Warehouse.objects.get(is_default=True)
    return StockItem.objects.get(part=part, warehouse=warehouse).on_hand


# --- Costo promedio ponderado (INV-08) -------------------------------------------


def test_successive_receipts_weighted_average(in_a, part):
    receive(in_a, part, 10, "100")
    assert avg(part) == D("100")
    movement = receive(in_a, part, 10, "200")
    assert avg(part) == D("150")
    assert movement.avg_cost_after == D("150") and movement.on_hand_after == D("20")
    no_cost = receive(in_a, part, 5)
    assert avg(part) == D("150")  # sin costo: entra al promedio vigente
    assert no_cost.unit_cost == D("150") and no_cost.total_cost == D("750.00")
    part.refresh_from_db()
    assert part.last_cost == D("200") and part.last_received_at is not None
    assert on_hand(part) == D("25")


def test_zero_cost_dilutes_the_average(in_a, part):
    receive(in_a, part, 10, "100")
    receive(in_a, part, 10, "0")
    assert avg(part) == D("50")


def test_negative_stock_then_receipt_sets_the_new_cost(in_a, part, wo):
    receive(in_a, part, 10, "100")
    out = issue(in_a, wo, part, 15)  # nunca se rechaza por falta de stock (Q1)
    assert out.quantity == D("-15") and out.unit_cost == D("100") and out.total_cost == D("-1500.00")
    assert on_hand(part) == D("-5")
    receive(in_a, part, 10, "80")
    assert avg(part) == D("80")  # Q ≤ 0 → el costo de la entrada
    assert on_hand(part) == D("5")


def test_return_recalculates_with_the_issue_cost(in_a, part, wo):
    receive(in_a, part, 10, "100")
    out = issue(in_a, wo, part, 4)
    receive(in_a, part, 10, "200")
    assert avg(part) == D("162.5")
    back = services.return_from_work_order(movement=out, by=in_a.tech.user)
    assert back.kind == StockMovement.Kind.RETURN and back.quantity == D("4") and back.unit_cost == D("100")
    assert back.reverses_id == out.pk and back.work_order_id == wo.pk
    assert avg(part) == D("150")
    # Una salida se devuelve una sola vez.
    assert services.return_from_work_order(movement=out, by=in_a.tech.user) == back
    assert StockMovement.objects.filter(kind=StockMovement.Kind.RETURN).count() == 1


def test_transfer_moves_stock_without_changing_the_average(in_a, part):
    main = services.ensure_default_warehouse()
    other = services.create_warehouse(membership=in_a.admin, code="B2", name="Bodega 2")
    receive(in_a, part, 10, "100")
    out, inbound = services.transfer_stock(
        membership=in_a.admin, part=part, from_warehouse=main, to_warehouse=other, quantity=4
    )
    assert (out.quantity, inbound.quantity) == (D("-4"), D("4"))
    assert out.transfer_group == inbound.transfer_group is not None
    assert out.unit_cost == inbound.unit_cost == D("100")
    assert avg(part) == D("100")
    assert on_hand(part, main) == D("6") and on_hand(part, other) == D("4")
    with pytest.raises(InventoryError):
        services.transfer_stock(
            membership=in_a.admin, part=part, from_warehouse=main, to_warehouse=main, quantity=1
        )


# --- Conteo (INV-06) ---------------------------------------------------------------


def test_count_with_no_difference_creates_nothing(in_a, part):
    receive(in_a, part, 10, "100")
    assert services.count_stock(membership=in_a.admin, part=part, counted_quantity=10) is None
    assert StockMovement.objects.filter(kind=StockMovement.Kind.ADJUSTMENT).count() == 0


def test_count_requires_a_reason_when_negative_or_over_ten_percent(in_a, part):
    receive(in_a, part, 10, "100")
    with pytest.raises(InventoryError):
        services.count_stock(membership=in_a.admin, part=part, counted_quantity=12)
    with pytest.raises(InventoryError):
        services.count_stock(membership=in_a.admin, part=part, counted_quantity="9,5")
    small = services.count_stock(membership=in_a.admin, part=part, counted_quantity=11)  # 10 %: sin motivo
    assert small.quantity == D("1") and small.unit_cost == D("100") and small.reason == "count"
    adjusted = services.count_stock(membership=in_a.admin, part=part, counted_quantity=8, note="Rotos")
    assert adjusted.quantity == D("-3") and adjusted.on_hand_after == D("8")
    assert avg(part) == D("100")
    assert AuditEvent.objects.filter(action=AuditEvent.Action.STOCK_ADJUST).count() == 2


def test_initial_count_with_cost_sets_the_average(in_a, part):
    movement = services.count_stock(membership=in_a.admin, part=part, counted_quantity=5, unit_cost="1200")
    assert movement.quantity == D("5") and avg(part) == D("1200")
    # Con stock positivo el costo informado no cambia el promedio.
    services.count_stock(membership=in_a.admin, part=part, counted_quantity=6, unit_cost="1", note="Hallado")
    assert avg(part) == D("1200")


# --- Idempotencia ----------------------------------------------------------------


def test_receipt_is_idempotent_by_client_uuid(in_a, part):
    key = uuid.uuid4()
    first = receive(in_a, part, 3, "10", client_uuid=key)
    assert receive(in_a, part, 3, "10", client_uuid=key) == first
    assert StockMovement.objects.count() == 1 and on_hand(part) == D("3")
    other = services.create_part(membership=in_a.admin, code="X", name="Otro")
    with pytest.raises(InventoryError) as error:
        receive(in_a, other, 3, client_uuid=key)
    assert error.value.code == "conflict"


def test_transfer_resend_returns_the_same_pair(in_a, part):
    main = services.ensure_default_warehouse()
    other = services.create_warehouse(membership=in_a.admin, code="B2", name="Bodega 2")
    key = uuid.uuid4()
    pair = services.transfer_stock(
        membership=in_a.admin, part=part, from_warehouse=main, to_warehouse=other, quantity=1, client_uuid=key
    )
    again = services.transfer_stock(
        membership=in_a.admin, part=part, from_warehouse=main, to_warehouse=other, quantity=1, client_uuid=key
    )
    assert again == pair and StockMovement.objects.count() == 2


def test_issue_resend_is_checked_before_the_work_order_state(in_a, part, wo):
    key = uuid.uuid4()
    first = issue(in_a, wo, part, 2, client_uuid=key)
    set_status(wo, WorkOrder.Status.CLOSED)
    assert issue(in_a, wo, part, 2, client_uuid=key) == first  # reenvío aunque la OT esté cerrada
    with pytest.raises(InventoryError) as error:
        issue(in_a, wo, part, 1)
    assert error.value.code == "locked"
    other = make_work_order(in_a, title="Otra")
    with pytest.raises(InventoryError) as error:
        issue(in_a, other, part, 2, client_uuid=key)
    assert error.value.code == "conflict"


def test_cancelled_work_order_rejects_issues(in_a, part, wo):
    set_status(wo, WorkOrder.Status.CANCELLED)
    with pytest.raises(InventoryError) as error:
        issue(in_a, wo, part, 1)
    assert error.value.code == "cancelled"


# --- Inmutabilidad (INV-14) --------------------------------------------------------


def test_movements_are_immutable(in_a, part):
    movement = receive(in_a, part, 1, "5")
    movement.note = "cambio"
    with pytest.raises(ImmutableMovementError):
        movement.save()
    with pytest.raises(ImmutableMovementError):
        movement.delete()
    with pytest.raises(ImmutableMovementError):
        StockMovement.objects.filter(pk=movement.pk).update(note="x")
    with pytest.raises(ImmutableMovementError):
        StockMovement.objects.filter(pk=movement.pk).delete()
    with pytest.raises(ImmutableMovementError):
        StockMovement.all_tenants.filter(pk=movement.pk).delete()


# --- Bodegas (INV-03) ----------------------------------------------------------------


def test_default_warehouse_is_created_once_and_lazily(in_a, part):
    assert not Warehouse.objects.exists()
    receive(in_a, part, 1)
    first = services.ensure_default_warehouse()
    assert services.ensure_default_warehouse() == first
    assert first.code == "BOD" and first.is_default and first.is_active
    assert Warehouse.objects.filter(is_default=True).count() == 1


def test_first_created_warehouse_becomes_default_and_default_can_change(in_a):
    first = services.create_warehouse(membership=in_a.admin, code="CEN", name="Central")
    second = services.create_warehouse(membership=in_a.admin, code="nor", name="Norte")
    assert first.is_default and not second.is_default
    with pytest.raises(InventoryError):
        services.create_warehouse(membership=in_a.admin, code="NOR", name="Duplicada")
    services.set_default_warehouse(membership=in_a.admin, warehouse=second)
    assert list(Warehouse.objects.filter(is_default=True)) == [second]
    assert services.ensure_default_warehouse() == second


def test_warehouse_cannot_be_deactivated_with_stock_reservations_or_as_default(in_a, part, wo):
    main = services.ensure_default_warehouse()
    other = services.create_warehouse(membership=in_a.admin, code="B2", name="Bodega 2")
    with pytest.raises(InventoryError):
        services.set_warehouse_active(membership=in_a.admin, warehouse=main, is_active=False)
    receive(in_a, part, 2, warehouse=other)
    with pytest.raises(InventoryError):
        services.set_warehouse_active(membership=in_a.admin, warehouse=other, is_active=False)
    services.transfer_stock(
        membership=in_a.admin, part=part, from_warehouse=other, to_warehouse=main, quantity=2
    )
    services.reserve_for_work_order(work_order=wo, part=part, quantity=1, warehouse=other, source="manual")
    with pytest.raises(InventoryError):
        services.set_warehouse_active(membership=in_a.admin, warehouse=other, is_active=False)
    services.release_work_order_reservations(work_order=wo, reason="cancelled")
    services.set_warehouse_active(membership=in_a.admin, warehouse=other, is_active=False)
    other.refresh_from_db()
    assert not other.is_active
    with pytest.raises(InventoryError):
        receive(in_a, part, 1, warehouse=other)
    assert AuditEvent.objects.filter(action=AuditEvent.Action.DEACTIVATE).count() == 1


def test_warehouse_for_prefers_reservation_then_nearest_location_then_default(in_a, part):
    site = Location.objects.create(code="PL", name="Planta")
    area = Location.objects.create(code="AR", name="Área", parent=site)
    line = Location.objects.create(code="LI", name="Línea", parent=area)
    in_a.asset.location = line
    in_a.asset.save()
    default = services.ensure_default_warehouse()
    wo = make_work_order(in_a)
    assert warehouse_for(work_order=wo) == default
    at_site = services.create_warehouse(membership=in_a.admin, code="PL", name="Planta", location=site)
    assert warehouse_for(work_order=wo) == at_site
    at_area = services.create_warehouse(membership=in_a.admin, code="AR", name="Área", location=area)
    assert warehouse_for(work_order=wo) == at_area
    assert warehouse_for(location=site) == at_site
    services.reserve_for_work_order(work_order=wo, part=part, quantity=1, warehouse=default, source="manual")
    assert warehouse_for(work_order=wo, part=part) == default
    out = issue(in_a, wo, part, 1)
    assert out.warehouse_id == default.pk


def test_stock_levels_and_list_statuses(in_a, part, wo):
    services.set_stock_levels(
        membership=in_a.admin, part=part, min_quantity=5, target_quantity=10, bin_location="A1"
    )
    with pytest.raises(InventoryError):
        services.set_stock_levels(membership=in_a.admin, part=part, min_quantity=5, target_quantity=2)
    receive(in_a, part, 6, "10")
    empty = services.create_part(membership=in_a.admin, code="SIN", name="Sin stock")
    negative = services.create_part(membership=in_a.admin, code="NEG", name="Negativo")
    issue(in_a, wo, negative, 1)
    codes = lambda **kw: [p.code for p in stock_list(membership=in_a.admin, **kw)]  # noqa: E731
    assert codes() == ["NEG", "ROD-6205", "SIN"]
    assert codes(status="below_min") == []
    services.reserve_for_work_order(work_order=wo, part=part, quantity=2, source="manual")
    assert codes(status="below_min") == ["ROD-6205"]
    assert codes(status="out") == ["SIN"]
    assert codes(status="negative") == ["NEG"]
    assert codes(q="6205") == ["ROD-6205"]
    row = stock_list(membership=in_a.admin, q="ROD").get()
    assert (row.on_hand_total, row.reserved_total, row.available_total) == (D("6"), D("2"), D("4"))
    services.set_part_active(membership=in_a.admin, part=empty, is_active=False)
    assert codes(status="inactive") == ["SIN"]
    detail = part_detail(part.pk, membership=in_a.admin)
    assert detail.on_hand_total == D("6") and detail.available_total == D("4")
    assert detail.show_costs and len(detail.movements) == 1
    assert not part_detail(part.pk, membership=in_a.tech).show_costs


def test_part_code_is_unique_ignoring_case_and_edits_are_audited(in_a, part):
    with pytest.raises(InventoryError) as error:
        services.create_part(membership=in_a.admin, code="rod-6205", name="Duplicado")
    assert "code" in error.value.message_dict
    other = services.create_part(membership=in_a.admin, code="OTRO", name="Otro", unit="")
    assert other.unit == "un"
    with pytest.raises(InventoryError):
        services.update_part(membership=in_a.admin, part=other, code="ROD-6205")
    services.update_part(membership=in_a.admin, part=other, name="Otro nombre", category="Correas")
    other.refresh_from_db()
    assert other.name == "Otro nombre"
    event = AuditEvent.objects.filter(action=AuditEvent.Action.UPDATE).first()
    assert event.changes["fields"]["name"] == ["Otro", "Otro nombre"]
    with pytest.raises(TypeError):
        services.update_part(membership=in_a.admin, part=other, avg_cost=1)


# --- Reservas (INV-10) -----------------------------------------------------------------


def test_reservations_sum_consume_fulfil_and_release(in_a, part, wo):
    receive(in_a, part, 10, "100")
    first = services.reserve_for_work_order(work_order=wo, part=part, quantity=3, source="plan")
    again = services.reserve_for_work_order(work_order=wo, part=part, quantity=2, source="manual")
    assert again.pk == first.pk and again.quantity == D("5")
    item = StockItem.objects.get(part=part)
    assert item.reserved == D("5")
    issue(in_a, wo, part, 2)
    first.refresh_from_db()
    assert first.consumed_quantity == D("2") and first.status == Reservation.Status.ACTIVE
    assert StockItem.objects.get(part=part).reserved == D("3")
    issue(in_a, wo, part, 4)  # consumir de más no crea otra reserva
    first.refresh_from_db()
    assert first.status == Reservation.Status.FULFILLED and first.pending == 0
    assert StockItem.objects.get(part=part).reserved == D("0")
    assert Reservation.objects.count() == 1
    assert AuditEvent.objects.filter(action=AuditEvent.Action.RESERVE).count() == 2


def test_release_on_close_cancel_and_manually(in_a, part, wo):
    other_part = services.create_part(membership=in_a.admin, code="COR", name="Correa")
    services.reserve_for_work_order(work_order=wo, part=part, quantity=3, source="manual")
    services.reserve_for_work_order(work_order=wo, part=other_part, quantity=1, source="manual")
    issue(in_a, wo, part, 1)
    assert services.release_work_order_reservations(work_order=wo, reason="closed") == 2
    assert set(Reservation.objects.values_list("status", "release_reason")) == {("released", "closed")}
    assert set(StockItem.objects.values_list("reserved", flat=True)) == {D("0")}
    assert services.release_work_order_reservations(work_order=wo, reason="closed") == 0

    wo2 = make_work_order(in_a, title="Otra")
    manual = services.reserve_for_work_order(work_order=wo2, part=part, quantity=2, source="manual")
    with pytest.raises(Exception):  # noqa: B017 - el técnico no gestiona reservas
        services.release_reservation(membership=in_a.tech, reservation=manual)
    released = services.release_reservation(membership=in_a.admin, reservation=manual)
    assert released.status == "released" and released.release_reason == "manual"
    with pytest.raises(InventoryError):
        services.release_reservation(membership=in_a.admin, reservation=manual)


def test_completed_work_order_rejects_new_reservations(in_a, part, wo):
    set_status(wo, WorkOrder.Status.COMPLETED)
    with pytest.raises(InventoryError) as error:
        services.reserve_for_work_order(work_order=wo, part=part, quantity=1, source="manual")
    assert error.value.code == "locked"


# --- Alerta de mínimo (INV-12 CA3) -------------------------------------------------------


def test_low_stock_alert_once_a_day_to_people_who_move_stock(
    in_a, part, wo, django_capture_on_commit_callbacks
):
    services.set_stock_levels(membership=in_a.admin, part=part, min_quantity=5)
    with django_capture_on_commit_callbacks(execute=True):
        receive(in_a, part, 10, "100")
    assert not Notification.objects.exists()
    with django_capture_on_commit_callbacks(execute=True):
        issue(in_a, wo, part, 6)  # disponible 4 < 5: cruza el umbral
    notes = list(Notification.objects.filter(kind=Kind.STOCK_LOW))
    assert [n.recipient_id for n in notes] == [in_a.admin.user_id]  # técnico y visualizador no mueven stock
    assert notes[0].dedupe_key.startswith(f"stock_low:{StockItem.objects.get(part=part).pk}:")
    with django_capture_on_commit_callbacks(execute=True):
        issue(in_a, wo, part, 1)  # sigue bajo: no es un cruce
        receive(in_a, part, 10)
        issue(in_a, wo, part, 10)  # vuelve a cruzar el mismo día: no se repite
    assert Notification.objects.filter(kind=Kind.STOCK_LOW).count() == 1


def test_low_stock_alert_is_not_sent_if_rolled_back(in_a, part, wo, django_capture_on_commit_callbacks):
    services.set_stock_levels(membership=in_a.admin, part=part, min_quantity=5)
    receive(in_a, part, 5)  # ya bajo mínimo al empezar no cuenta como cruce
    receive(in_a, part, 5)
    with django_capture_on_commit_callbacks(execute=False) as callbacks:
        issue(in_a, wo, part, 6)
    assert len(callbacks) == 1
    assert not Notification.objects.exists()


# --- Reconciliación y propiedad -----------------------------------------------------------


def test_reconcile_detects_and_fixes_drift(in_a, part, wo):
    receive(in_a, part, 5, "10")
    services.reserve_for_work_order(work_order=wo, part=part, quantity=2, source="manual")
    StockItem.objects.filter(part=part).update(on_hand=D("99"), reserved=D("0"))
    found = services.reconcile_stock()
    assert {m.kind for m in found} == {"on_hand", "reserved"}
    call_command("inventory_reconcile", "--organization", in_a.org.slug)  # sin --fix solo informa
    assert len(services.reconcile_stock()) == 2
    call_command("inventory_reconcile", "--organization", in_a.org.slug, "--fix")
    assert services.reconcile_stock() == []
    item = StockItem.objects.get(part=part)
    assert (item.on_hand, item.reserved) == (D("5"), D("2"))
    assert AuditEvent.objects.filter(action=AuditEvent.Action.STOCK_RECONCILE).count() == 2


@pytest.mark.parametrize("seed", [1, 7, 42])
def test_random_sequences_keep_balance_equal_to_the_ledger(in_a, seed):
    rng = random.Random(seed)
    parts = [
        services.create_part(membership=in_a.admin, code=f"P{i}", name=f"Repuesto {i}") for i in range(3)
    ]
    warehouses = [
        services.ensure_default_warehouse(),
        services.create_warehouse(membership=in_a.admin, code="B2", name="Bodega 2"),
    ]
    orders = [make_work_order(in_a, title=f"OT {i}") for i in range(2)]
    issues = []
    for _step in range(60):
        part, warehouse, order = rng.choice(parts), rng.choice(warehouses), rng.choice(orders)
        quantity = D(rng.randint(1, 8))
        operation = rng.choice(
            ["receive", "receive", "issue", "count", "transfer", "reserve", "return", "release"]
        )
        if operation == "receive":
            cost = rng.choice([None, "0", str(rng.randint(1, 500))])
            receive(in_a, part, quantity, cost, warehouse=warehouse)
        elif operation == "issue":
            issues.append(issue(in_a, order, part, quantity, warehouse=warehouse))
        elif operation == "count":
            services.count_stock(
                membership=in_a.admin,
                part=part,
                counted_quantity=rng.randint(0, 10),
                warehouse=warehouse,
                note="c",
            )
        elif operation == "transfer":
            target = warehouses[1] if warehouse == warehouses[0] else warehouses[0]
            services.transfer_stock(
                membership=in_a.admin,
                part=part,
                from_warehouse=warehouse,
                to_warehouse=target,
                quantity=quantity,
            )
        elif operation == "reserve":
            services.reserve_for_work_order(work_order=order, part=part, quantity=quantity, source="manual")
        elif operation == "return" and issues:
            services.return_from_work_order(movement=rng.choice(issues), by=in_a.admin.user)
        elif operation == "release":
            services.release_work_order_reservations(work_order=order, reason="manual")
    assert services.reconcile_stock() == []
    for part in parts:
        part.refresh_from_db()
        assert part.avg_cost >= 0
        for item in StockItem.objects.filter(part=part):
            last = (
                StockMovement.objects.filter(part=part, warehouse=item.warehouse_id)
                .order_by("-created_at")
                .first()
            )
            assert last is None or last.on_hand_after == item.on_hand


def test_visible_warehouses_lists_active_by_default(in_a):
    main = services.ensure_default_warehouse()
    other = services.create_warehouse(membership=in_a.admin, code="B2", name="Bodega 2")
    services.set_warehouse_active(membership=in_a.admin, warehouse=other, is_active=False)
    assert list(visible_warehouses(membership=in_a.tech)) == [main]
    assert set(visible_warehouses(membership=in_a.admin, include_inactive=True)) == {main, other}
