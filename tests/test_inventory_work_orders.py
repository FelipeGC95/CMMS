"""Repuestos del catálogo en la OT (INV-09, INV-10; decisión Q2) con base real y valores conocidos."""

import uuid
from decimal import Decimal

import pytest
from django.urls import reverse

from apps.audit.models import AuditEvent
from apps.core.tenancy import tenant_context
from apps.inventory import services as inventory
from apps.inventory.models import Reservation, StockItem, StockMovement, Warehouse
from apps.organizations.models import Location, Plan
from apps.organizations.modules import ModuleNotIncludedError
from apps.work.models import PartUsage, WorkOrder
from apps.work.selectors import parts_for_work_order, planned_parts
from apps.work.services import (
    WorkOrderError,
    add_part_usage,
    complete_work_order,
    remove_cost_line,
    transition,
)
from tests.conftest import login
from tests.inventory_utils import make_work_order, set_status

pytestmark = pytest.mark.usefixtures("reconciled_inventory")
D = Decimal
S = WorkOrder.Status


@pytest.fixture
def part(in_a):
    return inventory.create_part(membership=in_a.admin, code="ROD-6205", name="Rodamiento 6205", unit="un")


@pytest.fixture
def wo(in_a):
    return make_work_order(in_a)


def receive(in_a, part, quantity, cost=None, **kwargs):
    return inventory.receive_stock(
        membership=in_a.admin, part=part, quantity=quantity, unit_cost=cost, **kwargs
    )


def use(in_a, wo, part, quantity, **kwargs):
    return add_part_usage(work_order=wo, membership=in_a.tech, part=part, quantity=quantity, **kwargs)


def on_hand(part):
    return StockItem.objects.get(part=part, warehouse=Warehouse.objects.get(is_default=True)).on_hand


def avg(part):
    part.refresh_from_db()
    return part.avg_cost


# --- Consumo con costo promedio (INV-09) ------------------------------------------


def test_catalog_usage_costs_the_current_average_and_issues_stock(in_a, wo, part):
    receive(in_a, part, 10, "100")
    receive(in_a, part, 10, "200")  # promedio 150
    usage = use(in_a, wo, part, 2, unit_cost="1", description="ignorado", unit="kg")
    assert usage.description == "ROD-6205 · Rodamiento 6205" and usage.unit == "un"
    assert usage.unit_cost == D("150") and usage.cost == D("300.00")  # el costo del cliente se ignora
    movement = usage.stock_movement
    assert movement.kind == StockMovement.Kind.ISSUE and movement.quantity == D("-2")
    assert movement.work_order_id == wo.pk and usage.part_id == part.pk
    assert -movement.total_cost == usage.cost
    wo.refresh_from_db()
    assert wo.parts_cost == D("300.00") and wo.total_cost == D("300.00")
    assert on_hand(part) == D("18") and avg(part) == D("150")


def test_fractional_average_keeps_four_decimals(in_a, wo, part):
    receive(in_a, part, 3, "100")
    receive(in_a, part, 3, "100.01")  # promedio 100,005
    usage = use(in_a, wo, part, "1.5")
    assert usage.unit_cost == D("100.0050") and usage.cost == D("150.01")
    assert usage.cost == -usage.stock_movement.total_cost


def test_free_text_usage_is_unchanged(in_a, wo):
    usage = add_part_usage(
        work_order=wo,
        membership=in_a.tech,
        description=" Correa A-42 ",
        quantity=2,
        unit="un",
        unit_cost="12500",
    )
    assert (usage.description, usage.unit_cost, usage.cost) == ("Correa A-42", D("12500"), D("25000.00"))
    assert usage.part_id is None and usage.stock_movement_id is None
    assert not StockMovement.objects.exists()
    with pytest.raises(WorkOrderError):
        add_part_usage(work_order=wo, membership=in_a.tech, description="", quantity=1, unit_cost=1)
    with pytest.raises(WorkOrderError):
        add_part_usage(work_order=wo, membership=in_a.tech, description="Sin costo", quantity=1)


def test_usage_never_fails_for_lack_of_stock(in_a, wo, part):
    usage = use(in_a, wo, part, 3)
    assert usage.unit_cost == 0 and on_hand(part) == D("-3")


def test_usage_goes_out_of_the_nearest_warehouse(in_a, part):
    site = Location.objects.create(code="PLT", name="Planta")
    line = Location.objects.create(code="L1", name="Línea 1", kind="area", parent=site)
    in_a.asset.location = line
    in_a.asset.save()
    inventory.ensure_default_warehouse()
    plant = inventory.create_warehouse(membership=in_a.admin, code="BPL", name="Bodega planta", location=site)
    wo = make_work_order(in_a)
    usage = use(in_a, wo, part, 1)
    assert usage.stock_movement.warehouse_id == plant.pk
    chosen = use(in_a, wo, part, 1, warehouse=Warehouse.objects.get(is_default=True))
    assert chosen.stock_movement.warehouse.is_default


def test_usage_consumes_the_reservation(in_a, wo, part):
    receive(in_a, part, 10, "50")
    reservation = inventory.reserve_for_work_order(work_order=wo, part=part, quantity=3, source="manual")
    use(in_a, wo, part, 2)
    reservation.refresh_from_db()
    assert reservation.consumed_quantity == D("2") and reservation.status == Reservation.Status.ACTIVE
    use(in_a, wo, part, 2)  # de más: queda cubierta, no se crea otra reserva
    reservation.refresh_from_db()
    assert reservation.status == Reservation.Status.FULFILLED and reservation.consumed_quantity == D("4")
    assert Reservation.objects.count() == 1
    assert StockItem.objects.get(part=part).reserved == 0


# --- Quitar una línea enlazada (INV-09 CA6) --------------------------------------


def test_removing_a_linked_line_returns_the_stock_and_recalculates_the_average(in_a, wo, part):
    receive(in_a, part, 10, "100")
    usage = use(in_a, wo, part, 4)  # sale a 100
    receive(in_a, part, 6, "200")  # Q = 6 → (6·100 + 6·200) / 12 = 150
    assert avg(part) == D("150")
    reservation = inventory.reserve_for_work_order(work_order=wo, part=part, quantity=1, source="manual")
    remove_cost_line(line=usage, membership=in_a.tech)
    returned = StockMovement.objects.get(kind=StockMovement.Kind.RETURN)
    assert returned.reverses_id == usage.stock_movement_id and returned.unit_cost == D("100")
    assert returned.quantity == D("4") and on_hand(part) == D("16")
    assert avg(part) == D("137.5")  # (12·150 + 4·100) / 16
    assert not PartUsage.objects.exists()
    wo.refresh_from_db()
    assert wo.parts_cost == 0
    reservation.refresh_from_db()
    assert reservation.status == Reservation.Status.ACTIVE and reservation.consumed_quantity == 0
    assert AuditEvent.objects.filter(action=AuditEvent.Action.STOCK_RETURN).exists()


def test_removing_a_line_of_a_completed_but_open_work_order_still_returns(in_a, wo, part):
    in_a.org.require_close_approval = True
    in_a.org.save()
    wo.organization.refresh_from_db()
    usage = use(in_a, wo, part, 1)
    complete_work_order(work_order=wo, membership=in_a.tech, labor_minutes=0)
    wo.refresh_from_db()
    assert wo.status == S.COMPLETED
    remove_cost_line(line=usage, membership=in_a.tech)
    assert StockMovement.objects.filter(kind=StockMovement.Kind.RETURN).count() == 1


# --- Liberación al cerrar y al cancelar (INV-10) -------------------------------


def test_closing_releases_pending_reservations(in_a, wo, part):
    receive(in_a, part, 5, "10")
    reservation = inventory.reserve_for_work_order(work_order=wo, part=part, quantity=3, source="manual")
    use(in_a, wo, part, 1)
    complete_work_order(work_order=wo, membership=in_a.tech, labor_minutes=0)  # cierre automático
    reservation.refresh_from_db()
    assert reservation.status == Reservation.Status.RELEASED
    assert reservation.release_reason == Reservation.ReleaseReason.CLOSED
    assert StockItem.objects.get(part=part).reserved == 0


def test_approving_the_close_releases_too(in_a, wo, part):
    in_a.org.require_close_approval = True
    in_a.org.save()
    wo.organization.refresh_from_db()
    reservation = inventory.reserve_for_work_order(work_order=wo, part=part, quantity=2, source="manual")
    complete_work_order(work_order=wo, membership=in_a.tech, labor_minutes=0)
    reservation.refresh_from_db()
    assert reservation.status == Reservation.Status.ACTIVE  # completada, aún no cerrada
    transition(work_order=wo, to_status=S.CLOSED, membership=in_a.admin)
    reservation.refresh_from_db()
    assert reservation.release_reason == Reservation.ReleaseReason.CLOSED


def test_cancelling_releases_reservations(in_a, wo, part):
    reservation = inventory.reserve_for_work_order(work_order=wo, part=part, quantity=2, source="manual")
    transition(work_order=wo, to_status=S.CANCELLED, membership=in_a.admin)
    reservation.refresh_from_db()
    assert reservation.status == Reservation.Status.RELEASED
    assert reservation.release_reason == Reservation.ReleaseReason.CANCELLED
    assert StockItem.objects.get(part=part).reserved == 0


# --- Idempotencia (INV-09 CA8) ----------------------------------------------------


@pytest.mark.parametrize("catalog", [True, False])
def test_resending_returns_the_same_line_even_if_the_work_order_closed(in_a, wo, part, catalog):
    key = uuid.uuid4()
    line = {"part": part} if catalog else {"description": "Filtro", "unit_cost": 100}
    first = add_part_usage(work_order=wo, membership=in_a.tech, quantity=1, client_uuid=key, **line)
    complete_work_order(work_order=wo, membership=in_a.tech, labor_minutes=0)
    wo.refresh_from_db()
    assert wo.status == S.CLOSED
    again = add_part_usage(work_order=wo, membership=in_a.tech, quantity=1, client_uuid=key, **line)
    assert again.pk == first.pk and PartUsage.objects.count() == 1
    assert StockMovement.objects.count() == (1 if catalog else 0)
    with pytest.raises(WorkOrderError, match="cerrada"):
        add_part_usage(work_order=wo, membership=in_a.tech, quantity=1, client_uuid=uuid.uuid4(), **line)


def test_same_client_uuid_on_another_work_order_is_a_conflict(in_a, wo, part):
    key = uuid.uuid4()
    use(in_a, wo, part, 1, client_uuid=key)
    other = make_work_order(in_a, title="Otra")
    with pytest.raises(WorkOrderError) as error:
        use(in_a, other, part, 1, client_uuid=key)
    assert error.value.code == "conflict"
    assert PartUsage.objects.count() == 1 and StockMovement.objects.count() == 1


def test_cancelled_work_order_rejects_usage_with_cancelled_code(in_a, wo, part):
    set_status(wo, S.CANCELLED)
    with pytest.raises(WorkOrderError) as error:
        use(in_a, wo, part, 1)
    assert error.value.code == "cancelled"


# --- Módulo del plan (INV-16) -----------------------------------------------------


def test_catalog_part_without_inventory_module_is_refused_with_plan_message(in_a, wo, part):
    in_a.org.plan = Plan.objects.create(code="basico-x", name="Básico", features=["assets", "work_orders"])
    in_a.org.save(update_fields=["plan"])
    wo.organization.refresh_from_db()
    with pytest.raises(ModuleNotIncludedError) as error:
        use(in_a, wo, part, 1)
    assert "Básico" in error.value.message and "Inventario" in error.value.message
    usage = add_part_usage(work_order=wo, membership=in_a.tech, description="Texto", quantity=1, unit_cost=5)
    assert usage.part_id is None  # el texto libre sigue


# --- Completar con previstos pre-marcados (Q2) ------------------------------------


def test_complete_work_order_consumes_parts_used_in_the_same_transaction(in_a, wo, part):
    receive(in_a, part, 10, "40")
    reservation = inventory.reserve_for_work_order(work_order=wo, part=part, quantity=2, source="manual")
    complete_work_order(
        work_order=wo, membership=in_a.tech, labor_minutes=0, parts_used=[{"part": part, "quantity": D("2")}]
    )
    wo.refresh_from_db()
    assert wo.status == S.CLOSED and wo.parts_cost == D("80.00")
    reservation.refresh_from_db()
    assert reservation.status == Reservation.Status.FULFILLED
    assert on_hand(part) == D("8")


def test_failed_completion_rolls_back_the_consumption(in_a, wo, part):
    in_a.org.require_failure_details = True
    in_a.org.save()
    wo.organization.refresh_from_db()
    with pytest.raises(WorkOrderError):
        complete_work_order(
            work_order=wo, membership=in_a.tech, labor_minutes=0, parts_used=[{"part": part, "quantity": 1}]
        )
    assert not PartUsage.objects.exists() and not StockMovement.objects.exists()


def _complete_post(wo, **extra):
    return {"labor_minutes": "10", "impact": "none", "notes": "ok", **extra}


def test_complete_form_shows_pending_planned_parts_pre_marked(client, tenant_a):
    with tenant_context(tenant_a.org):
        part = inventory.create_part(membership=tenant_a.admin, code="FIL-1", name="Filtro aceite")
        other = inventory.create_part(membership=tenant_a.admin, code="COR-1", name="Correa")
        wo = make_work_order(tenant_a)
        set_status(wo, S.IN_PROGRESS)
        res = inventory.reserve_for_work_order(work_order=wo, part=part, quantity=2, source="manual")
        res_other = inventory.reserve_for_work_order(work_order=wo, part=other, quantity=1, source="manual")
    login(client, tenant_a.tech)
    page = client.get(reverse("work_order_detail", args=[wo.pk])).content.decode()
    assert "Repuestos previstos usados" in page
    assert f'name="planned-use_{res.pk.hex}"' in page and "checked" in page
    assert f'name="planned-qty_{res.pk.hex}"' in page

    response = client.post(
        reverse("work_order_complete", args=[wo.pk]),
        _complete_post(
            wo,
            **{
                f"planned-use_{res.pk.hex}": "on",
                f"planned-qty_{res.pk.hex}": "1.5",
                f"planned-qty_{res_other.pk.hex}": "1",  # desmarcado: no se consume
            },
        ),
    )
    assert response.status_code == 302
    with tenant_context(tenant_a.org):
        wo.refresh_from_db()
        assert wo.status == S.CLOSED
        usage = PartUsage.objects.get()
        assert usage.part_id == part.pk and usage.quantity == D("1.5")
        res_other.refresh_from_db()
        assert res_other.consumed_quantity == 0 and res_other.status == Reservation.Status.RELEASED


def test_complete_form_requires_quantity_for_a_marked_part(client, tenant_a):
    with tenant_context(tenant_a.org):
        part = inventory.create_part(membership=tenant_a.admin, code="FIL-1", name="Filtro aceite")
        wo = make_work_order(tenant_a)
        set_status(wo, S.IN_PROGRESS)
        res = inventory.reserve_for_work_order(work_order=wo, part=part, quantity=2, source="manual")
    login(client, tenant_a.tech)
    response = client.post(
        reverse("work_order_complete", args=[wo.pk]),
        _complete_post(wo, **{f"planned-use_{res.pk.hex}": "on", f"planned-qty_{res.pk.hex}": ""}),
    )
    assert response.status_code == 400
    assert "Indica la cantidad usada" in response.content.decode()
    with tenant_context(tenant_a.org):
        assert WorkOrder.objects.get(pk=wo.pk).status == S.IN_PROGRESS


# --- Detalle de la OT: previstos, buscador, Usar, reservar y quitar ---------------


def test_detail_shows_planned_parts_with_shortfall_and_use(client, tenant_a):
    with tenant_context(tenant_a.org):
        part = inventory.create_part(membership=tenant_a.admin, code="ROD-1", name="Rodamiento")
        inventory.receive_stock(membership=tenant_a.admin, part=part, quantity=1, unit_cost="10")
        wo = make_work_order(tenant_a)
        inventory.reserve_for_work_order(work_order=wo, part=part, quantity=3, source="manual")
        [planned] = planned_parts(work_order=wo)
        assert planned.shortfall == D("2") and planned.pending == D("3")
    login(client, tenant_a.tech)
    page = client.get(reverse("work_order_detail", args=[wo.pk])).content.decode()
    assert "Repuestos previstos" in page and "Faltan 2" in page
    assert "Usar" in page and reverse("inventory_wo_reserve", args=[wo.pk]) not in page  # técnico: no reserva
    assert "No está en el catálogo" in page

    response = client.post(
        reverse("work_order_part_add", args=[wo.pk]),
        {"part": str(part.pk), "quantity": "3", "client_uuid": str(uuid.uuid4()), "unit_cost": "999"},
    )
    assert response.status_code == 302 and response["Location"].endswith("#costs")
    with tenant_context(tenant_a.org):
        usage = PartUsage.objects.get()
        assert usage.unit_cost == D("10") and usage.quantity == D("3")
        assert Reservation.objects.get().status == Reservation.Status.FULFILLED


def test_search_lists_suggestions_first_without_costs(client, tenant_a):
    with tenant_context(tenant_a.org):
        reserved = inventory.create_part(membership=tenant_a.admin, code="Z-RES", name="Reservado")
        used = inventory.create_part(membership=tenant_a.admin, code="Y-USE", name="Usado antes")
        plain = inventory.create_part(membership=tenant_a.admin, code="A-PLN", name="Otro")
        inactive = inventory.create_part(membership=tenant_a.admin, code="A-OFF", name="Otro inactivo")
        inventory.set_part_active(membership=tenant_a.admin, part=inactive, is_active=False)
        inventory.receive_stock(membership=tenant_a.admin, part=plain, quantity=4, unit_cost="12345")
        before = make_work_order(tenant_a, title="Antes")
        add_part_usage(work_order=before, membership=tenant_a.tech, part=used, quantity=1)
        wo = make_work_order(tenant_a)
        inventory.reserve_for_work_order(work_order=wo, part=reserved, quantity=1, source="manual")
        suggested = parts_for_work_order(work_order=wo, membership=tenant_a.tech)
        assert [p.code for p in suggested] == ["Z-RES", "Y-USE"]
        found = parts_for_work_order(work_order=wo, membership=tenant_a.tech, q="o")
        assert [p.code for p in found] == ["Z-RES", "Y-USE", "A-PLN"]  # sugeridos primero, sin inactivos
        assert found[2].available_total == D("4")
    login(client, tenant_a.tech)
    url = reverse("inventory_wo_part_search", args=[wo.pk])
    html = client.get(url, {"part_q": "otro"}, HTTP_HX_REQUEST="true").content.decode()
    assert "A-PLN" in html and "A-OFF" not in html and "12345" not in html and "12.345" not in html
    fallback = client.get(reverse("work_order_detail", args=[wo.pk]), {"part_q": "otro"}).content.decode()
    assert "A-PLN" in fallback  # sin JS: el buscador recarga el detalle


def test_manager_reserves_and_releases_from_the_detail(client, tenant_a):
    with tenant_context(tenant_a.org):
        part = inventory.create_part(membership=tenant_a.admin, code="ROD-1", name="Rodamiento")
        wo = make_work_order(tenant_a)
    login(client, tenant_a.admin)
    page = client.get(reverse("work_order_detail", args=[wo.pk]), {"part_q": "ROD"}).content.decode()
    assert "Reservar" in page and reverse("inventory_wo_reserve", args=[wo.pk]) in page
    response = client.post(
        reverse("inventory_wo_reserve", args=[wo.pk]), {"part": str(part.pk), "quantity": "2"}
    )
    assert response.status_code == 302 and response["Location"].endswith("#parts")
    client.post(reverse("inventory_wo_reserve", args=[wo.pk]), {"part": str(part.pk), "quantity": "1"})
    with tenant_context(tenant_a.org):
        reservation = Reservation.objects.get()
        assert reservation.quantity == D("3") and reservation.source == Reservation.Source.MANUAL
        assert reservation.created_by == tenant_a.admin.user
    client.post(reverse("inventory_wo_release", args=[wo.pk, reservation.pk]))
    with tenant_context(tenant_a.org):
        reservation.refresh_from_db()
        assert reservation.status == Reservation.Status.RELEASED
        assert reservation.release_reason == Reservation.ReleaseReason.MANUAL
        assert StockItem.objects.get(part=part).reserved == 0


def test_removing_a_linked_line_from_the_detail_returns_stock(client, tenant_a):
    with tenant_context(tenant_a.org):
        part = inventory.create_part(membership=tenant_a.admin, code="ROD-1", name="Rodamiento")
        inventory.receive_stock(membership=tenant_a.admin, part=part, quantity=2, unit_cost="10")
        wo = make_work_order(tenant_a)
        usage = add_part_usage(work_order=wo, membership=tenant_a.tech, part=part, quantity=1)
    login(client, tenant_a.tech)
    client.post(reverse("work_order_cost_delete", args=[wo.pk, "part", usage.pk]))
    with tenant_context(tenant_a.org):
        assert not PartUsage.objects.exists()
        assert StockItem.objects.get(part=part).on_hand == D("2")


def test_without_inventory_module_the_detail_hides_catalog_and_keeps_free_text(client, tenant_a):
    tenant_a.org.plan = Plan.objects.create(
        code="basico-x", name="Básico", features=["assets", "work_orders"]
    )
    tenant_a.org.save(update_fields=["plan"])
    with tenant_context(tenant_a.org):
        part = inventory.create_part(membership=tenant_a.admin, code="ROD-1", name="Rodamiento")
        wo = make_work_order(tenant_a)
        set_status(wo, S.IN_PROGRESS)
        inventory.reserve_for_work_order(work_order=wo, part=part, quantity=1, source="manual")
    login(client, tenant_a.admin)
    page = client.get(reverse("work_order_detail", args=[wo.pk])).content.decode()
    assert "Repuestos previstos" not in page and "/inventory/" not in page
    assert "Agregar repuesto o material" in page
    response = client.post(
        reverse("work_order_part_add", args=[wo.pk]), {"part": str(part.pk), "quantity": "1"}, follow=True
    )
    assert "no está incluido en tu plan" in response.content.decode()
    assert client.get(reverse("inventory_wo_part_search", args=[wo.pk])).status_code == 403
    with tenant_context(tenant_a.org):
        assert not PartUsage.objects.exists()
    response = client.post(reverse("work_order_complete", args=[wo.pk]), _complete_post(wo))
    assert response.status_code == 302
    with tenant_context(tenant_a.org):
        assert not PartUsage.objects.exists()  # sin módulo no hay pre-marcados


# --- Reconciliación (INV-14) ------------------------------------------------------


def test_reconcile_is_clean_after_the_work_order_flows(in_a, part):
    from django.core.management import call_command

    receive(in_a, part, 5, "20")
    first = make_work_order(in_a)
    inventory.reserve_for_work_order(work_order=first, part=part, quantity=4, source="manual")
    line = use(in_a, first, part, 2)
    remove_cost_line(line=line, membership=in_a.tech)
    use(in_a, first, part, 1)
    complete_work_order(work_order=first, membership=in_a.tech, labor_minutes=0)
    second = make_work_order(in_a, title="Otra")
    inventory.reserve_for_work_order(work_order=second, part=part, quantity=2, source="manual")
    transition(work_order=second, to_status=S.CANCELLED, membership=in_a.admin)
    assert inventory.reconcile_stock() == []
    call_command("inventory_reconcile")
