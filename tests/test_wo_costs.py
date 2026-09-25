"""Mano de obra, repuestos y costos de la OT (WO-04)."""

import uuid
from decimal import Decimal

import pytest
from django.urls import reverse

from apps.accounts.models import MembershipLocation
from apps.accounts.permissions import PermissionDeniedError
from apps.core.tenancy import tenant_context
from apps.organizations.models import Location
from apps.work.models import LaborRate, OtherCost, PartUsage, WorkOrder
from apps.work.services import (
    WorkOrderError,
    add_other_cost,
    add_part_usage,
    complete_work_order,
    create_work_order,
    hourly_cost_for,
    log_labor,
    remove_cost_line,
)
from tests.conftest import login


@pytest.fixture
def wo(in_a):
    return create_work_order(work_type="corrective", title="Cambio de rodamiento", asset=in_a.asset)


# --- Tarifas --------------------------------------------------------------------


def test_rate_is_personal_then_general_then_zero(in_a):
    assert hourly_cost_for(in_a.tech) == 0
    LaborRate.objects.create(hourly_cost=Decimal("15000"))
    assert hourly_cost_for(in_a.tech) == Decimal("15000")
    LaborRate.objects.create(membership=in_a.tech, hourly_cost=Decimal("20000"))
    assert hourly_cost_for(in_a.tech) == Decimal("20000")
    assert hourly_cost_for(in_a.admin) == Decimal("15000")


def test_labor_copies_the_rate_so_later_changes_do_not_alter_it(in_a, wo):
    rate = LaborRate.objects.create(hourly_cost=Decimal("12000"))
    entry = log_labor(work_order=wo, membership=in_a.tech, minutes=90)
    rate.hourly_cost = Decimal("99999")
    rate.save()
    entry.refresh_from_db()
    wo.refresh_from_db()
    assert entry.hourly_cost == Decimal("12000") and entry.cost == Decimal("18000.00")
    assert wo.labor_cost == Decimal("18000.00")


# --- Totales ----------------------------------------------------------------------


def test_totals_add_labor_parts_and_other_costs(in_a, wo):
    LaborRate.objects.create(hourly_cost=Decimal("10000"))
    log_labor(work_order=wo, membership=in_a.tech, minutes=30)
    add_part_usage(
        work_order=wo, membership=in_a.tech, description="Rodamiento 6205", quantity=2, unit="un",
        unit_cost=Decimal("4500"),
    )  # fmt: skip
    add_other_cost(
        work_order=wo, membership=in_a.admin, description="Rectificado eje", amount=Decimal("35000")
    )
    wo.refresh_from_db()
    assert (wo.labor_cost, wo.parts_cost, wo.other_cost) == (
        Decimal("5000.00"),
        Decimal("9000.00"),
        Decimal("35000.00"),
    )
    assert wo.total_cost == Decimal("49000.00")

    remove_cost_line(line=wo.parts.get(), membership=in_a.tech)
    wo.refresh_from_db()
    assert wo.parts_cost == 0 and wo.total_cost == Decimal("40000.00")


def test_completing_adds_labor_and_freezes_costs(in_a, wo):
    LaborRate.objects.create(hourly_cost=Decimal("6000"))
    add_part_usage(work_order=wo, membership=in_a.tech, description="Grasa", quantity="0.5", unit_cost=2000)
    complete_work_order(work_order=wo, membership=in_a.tech, labor_minutes=60)
    wo.refresh_from_db()
    assert wo.status == WorkOrder.Status.CLOSED
    assert wo.total_cost == Decimal("7000.00")
    with pytest.raises(WorkOrderError, match="cerrada"):
        add_part_usage(work_order=wo, membership=in_a.tech, description="X", quantity=1, unit_cost=1)
    with pytest.raises(WorkOrderError, match="cerrada"):
        remove_cost_line(line=wo.parts.get(), membership=in_a.tech)


def test_invalid_amounts_are_rejected(in_a, wo):
    with pytest.raises(WorkOrderError):
        add_part_usage(work_order=wo, membership=in_a.tech, description="X", quantity=0, unit_cost=1)
    with pytest.raises(WorkOrderError):
        add_other_cost(work_order=wo, membership=in_a.admin, description="X", amount=-1)
    with pytest.raises(WorkOrderError):
        log_labor(work_order=wo, membership=in_a.tech, minutes=0)


def test_part_usage_is_idempotent_by_client_uuid(in_a, wo):
    key = uuid.uuid4()
    line = {"description": "Filtro", "quantity": 1, "unit_cost": 100, "client_uuid": key}
    first = add_part_usage(work_order=wo, membership=in_a.tech, **line)
    second = add_part_usage(work_order=wo, membership=in_a.tech, **line)
    assert first.pk == second.pk and PartUsage.objects.count() == 1


# --- Permisos ---------------------------------------------------------------------


def test_permissions_by_role(in_a, wo):
    with pytest.raises(PermissionDeniedError):
        add_part_usage(work_order=wo, membership=in_a.viewer, description="X", quantity=1, unit_cost=1)
    with pytest.raises(PermissionDeniedError):
        log_labor(work_order=wo, membership=in_a.viewer, minutes=10)
    with pytest.raises(PermissionDeniedError):
        add_other_cost(work_order=wo, membership=in_a.tech, description="Servicio", amount=1)
    cost = add_other_cost(work_order=wo, membership=in_a.admin, description="Servicio", amount=1)
    with pytest.raises(PermissionDeniedError):
        remove_cost_line(line=cost, membership=in_a.tech)


# --- Vistas -----------------------------------------------------------------------


def test_cost_views_flow(client, tenant_a):
    with tenant_context(tenant_a.org):
        wo = create_work_order(work_type="corrective", title="x", asset=tenant_a.asset)
        LaborRate.objects.create(hourly_cost=Decimal("8000"))
    login(client, tenant_a.tech)
    detail = reverse("work_order_detail", args=[wo.pk])
    ok = client.post(
        reverse("work_order_part_add", args=[wo.pk]),
        {"description": "Correa A-42", "quantity": "1", "unit": "un", "unit_cost": "12500"},
    )
    assert ok.status_code == 302 and ok["Location"].endswith("#costs")
    client.post(reverse("work_order_labor_add", args=[wo.pk]), {"minutes": "45"})
    page = client.get(detail).content.decode()
    assert "Correa A-42" in page and "$ 18.500" in page  # 12.500 + 45 min × 8.000/h
    # El técnico no ve el formulario de otros costos ni puede usarlo.
    assert reverse("work_order_other_cost_add", args=[wo.pk]) not in page
    assert (
        client.post(
            reverse("work_order_other_cost_add", args=[wo.pk]), {"description": "x", "amount": "1"}
        ).status_code
        == 403
    )

    with tenant_context(tenant_a.org):
        part = PartUsage.objects.get()
    client.post(reverse("work_order_cost_delete", args=[wo.pk, "part", part.pk]))
    with tenant_context(tenant_a.org):
        assert not PartUsage.objects.exists()
        assert WorkOrder.objects.get(pk=wo.pk).total_cost == Decimal("6000.00")


def test_viewer_cannot_post_costs(client, tenant_a):
    with tenant_context(tenant_a.org):
        wo = create_work_order(work_type="corrective", title="x", asset=tenant_a.asset)
    login(client, tenant_a.viewer)
    for name, data in [
        ("work_order_part_add", {"description": "x", "quantity": "1", "unit_cost": "1"}),
        ("work_order_labor_add", {"minutes": "10"}),
    ]:
        assert client.post(reverse(name, args=[wo.pk]), data).status_code == 403


def test_scoped_member_cannot_add_costs_outside_their_sites(client, tenant_a):
    with tenant_context(tenant_a.org):
        allowed = Location.objects.create(code="PLT", name="Planta")
        wo = create_work_order(
            work_type="corrective", title="x", asset=tenant_a.asset
        )  # activo sin ubicación
        tech = tenant_a.tech
        tech.restricted_to_locations = True
        tech.save()
        MembershipLocation.objects.create(membership=tech, location=allowed)
    login(client, tenant_a.tech)
    response = client.post(reverse("work_order_labor_add", args=[wo.pk]), {"minutes": "10"})
    assert response.status_code == 404


def test_other_cost_line_can_be_removed_by_manager(client, tenant_a):
    with tenant_context(tenant_a.org):
        wo = create_work_order(work_type="corrective", title="x", asset=tenant_a.asset)
        cost = add_other_cost(
            work_order=wo, membership=tenant_a.admin, description="Grúa externa", amount=50000
        )
    login(client, tenant_a.admin)
    client.post(reverse("work_order_cost_delete", args=[wo.pk, "other", cost.pk]))
    with tenant_context(tenant_a.org):
        assert not OtherCost.objects.exists()
    assert client.post(reverse("work_order_cost_delete", args=[wo.pk, "otro", cost.pk])).status_code == 404
