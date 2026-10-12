"""Indicadores de inventario (INV-15) y "Dónde se usa" (INV-01 CA4), con fixtures de valor conocido."""

import datetime
from decimal import Decimal

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.core.tenancy import tenant_context
from apps.inventory import services
from apps.inventory.models import StockMovement
from apps.inventory.selectors import (
    InventoryInsights,
    consumption_12m,
    inventory_insights,
    inventory_value,
    part_where_used,
    stale_parts,
)
from apps.maintenance.models import PlanResource
from apps.maintenance.services import create_plan, publish_version, start_draft
from apps.work.models import PartUsage
from apps.work.services import add_part_usage
from tests.conftest import login
from tests.inventory_utils import make_work_order

pytestmark = pytest.mark.usefixtures("reconciled_inventory")

LONG_AGO = datetime.timedelta(days=400)


def backdate(movement, delta=LONG_AGO):
    """Lleva un movimiento al pasado (el libro es inmutable para la app; solo en tests)."""
    StockMovement._base_manager.filter(pk=movement.pk).update(occurred_at=timezone.now() - delta)


@pytest.fixture
def ledger(tenant_a):
    """Valores conocidos.

    A: 10 a $100 + 10 a $200 → promedio $150; 5 a la bodega B2. Valor 15×150 + 5×150 = $3.000.
    B: 4 a $50; salidas de 2 y 4 a OT ($300); se devuelve la de 2 ($100). Existencia 0. Consumo $200.
    C: 3 a $1.000 hace 400 días y nada más: sin movimiento en 12 meses, valor $3.000.
    D: 5 a $10; una salida de 1 hace 400 días (fuera de los 12 meses). Valor $40.
    E: 2 unidades sin costo y una salida de 3 → existencia −1: lo negativo no suma valor.
    Total $6.040 (principal $5.290, B2 $750); consumo de 12 meses $200 (+ $0 de E).
    """
    with tenant_context(tenant_a.org):
        a = tenant_a.admin
        main = services.ensure_default_warehouse()
        b2 = services.create_warehouse(membership=a, code="B2", name="Bodega 2")
        part_a = services.create_part(membership=a, code="A", name="Rodamiento")
        part_b = services.create_part(membership=a, code="B", name="Correa")
        part_c = services.create_part(membership=a, code="C", name="Motor de repuesto")
        part_d = services.create_part(membership=a, code="D", name="Fusible")
        part_e = services.create_part(membership=a, code="E", name="Abrazadera")
        services.receive_stock(membership=a, part=part_a, quantity=10, unit_cost="100")
        services.receive_stock(membership=a, part=part_a, quantity=10, unit_cost="200")
        services.transfer_stock(membership=a, part=part_a, from_warehouse=main, to_warehouse=b2, quantity=5)
        services.receive_stock(membership=a, part=part_b, quantity=4, unit_cost="50")
        wo = make_work_order(tenant_a)
        first = services.issue_to_work_order(work_order=wo, part=part_b, quantity=2, by=a.user)
        services.issue_to_work_order(work_order=wo, part=part_b, quantity=4, by=a.user)
        services.return_from_work_order(movement=first, by=a.user)
        backdate(services.receive_stock(membership=a, part=part_c, quantity=3, unit_cost="1000"))
        services.receive_stock(membership=a, part=part_d, quantity=5, unit_cost="10")
        backdate(services.issue_to_work_order(work_order=wo, part=part_d, quantity=1, by=a.user))
        services.receive_stock(membership=a, part=part_e, quantity=2)
        services.issue_to_work_order(work_order=wo, part=part_e, quantity=3, by=a.user)
    return {"main": main, "b2": b2, "a": part_a, "b": part_b, "c": part_c, "d": part_d, "e": part_e}


def test_inventory_value_total_and_by_warehouse(tenant_a, ledger):
    with tenant_context(tenant_a.org):
        total, rows = inventory_value(membership=tenant_a.admin)
    assert total == Decimal("6040.00")
    assert [(row.code, row.value) for row in rows] == [("BOD", Decimal("5290.00")), ("B2", Decimal("750.00"))]


def test_consumption_12m_is_issues_minus_returns_within_the_year(tenant_a, ledger):
    with tenant_context(tenant_a.org):
        assert consumption_12m(membership=tenant_a.admin) == Decimal("200.00")
        # Mirado desde 2 años adelante ya no queda nada en la ventana.
        later = timezone.now() + datetime.timedelta(days=730)
        assert consumption_12m(membership=tenant_a.admin, now=later) == 0


def test_stale_parts_have_stock_and_no_movement_in_12_months(tenant_a, ledger):
    with tenant_context(tenant_a.org):
        stale = list(stale_parts(membership=tenant_a.admin))
    assert [(part.code, part.stock_value) for part in stale] == [("C", Decimal("3000"))]
    assert stale[0].last_moved_at < timezone.now() - datetime.timedelta(days=365)


def test_insights_bundle_and_turnover(tenant_a, ledger):
    with tenant_context(tenant_a.org):
        insights = inventory_insights(membership=tenant_a.admin)
    assert insights.total_value == Decimal("6040.00")
    assert insights.consumption == Decimal("200.00")
    assert (insights.stale_count, insights.stale_value) == (1, Decimal("3000.00"))
    assert [(part.code, value) for part, value in insights.stale] == [("C", Decimal("3000.00"))]
    assert insights.turnover == Decimal("0.0")  # 200 / 6.040 ≈ 0,03


@pytest.mark.parametrize(
    ("consumption", "value", "expected"),
    [("2500", "1000", Decimal("2.5")), ("100", "300", Decimal("0.3")), ("100", "0", None)],
)
def test_turnover_is_consumption_over_current_value(consumption, value, expected):
    insights = InventoryInsights(
        total_value=Decimal(value),
        by_warehouse=[],
        consumption=Decimal(consumption),
        stale_count=0,
        stale_value=Decimal(0),
        stale=[],
    )
    assert insights.turnover == expected


def test_insights_empty_inventory_are_zero(tenant_a):
    with tenant_context(tenant_a.org):
        insights = inventory_insights(membership=tenant_a.admin)
    assert (insights.total_value, insights.consumption, insights.stale_count) == (0, 0, 0)
    assert insights.turnover is None


def test_home_shows_insights_with_cost_view(client, tenant_a, ledger):
    login(client, tenant_a.admin)
    body = client.get(reverse("inventory_home")).content.decode()
    assert "Valor del inventario" in body
    assert "$ 6.040" in body
    assert "Rotación aprox." in body
    assert "Fórmula" in body
    assert "Bodega 2" in body  # desglose por bodega
    assert reverse("inventory_part_detail", args=[ledger["c"].pk]) in body  # sin movimiento


# --- Dónde se usa ----------------------------------------------------------------


def _plan_with_part(tenant, part, *, code, quantity=2, asset_class=None):
    return create_plan(
        code=code,
        name=f"Plan {code}",
        asset_class=asset_class,
        calendar_interval=30,
        items=[{"text": "Revisar", "item_type": "check"}],
        resources=[
            {"kind": "part", "description": part.name, "quantity": quantity, "unit": "un", "part": part}
        ],
    )


def test_where_used_lists_plans_of_the_current_version_and_their_class(tenant_a):
    with tenant_context(tenant_a.org):
        a = tenant_a.admin
        part = services.create_part(membership=a, code="FIL", name="Filtro")
        current = _plan_with_part(tenant_a, part, code="PMA", asset_class=tenant_a.asset.asset_class)
        # Dos recursos del mismo repuesto en un plan se suman.
        PlanResource.objects.create(
            version=start_draft(plan=current), kind="part", description="Filtro extra", quantity=1, part=part
        )
        publish_version(version=current.versions.get(status="draft"))
        retired = _plan_with_part(tenant_a, part, code="PMB")
        retired.is_active = False
        retired.save()
        # Plan cuya versión vigente ya no lo prevé (lo tenía la versión 1).
        superseded = _plan_with_part(tenant_a, part, code="PMC")
        draft = start_draft(plan=superseded)
        draft.resources.all().delete()
        publish_version(version=draft)
        # Plan con el repuesto solo en un borrador.
        drafted = create_plan(
            code="PMD", name="Plan D", calendar_interval=30, items=[{"text": "x", "item_type": "check"}]
        )
        PlanResource.objects.create(
            version=start_draft(plan=drafted), kind="part", description="Filtro", quantity=1, part=part
        )
        where = part_where_used(part, membership=a)
    assert [(use.plan.code, use.quantity) for use in where.plans] == [("PMA", Decimal("3"))]
    assert where.plans[0].plan.asset_class.name == "Compresor"


def test_where_used_lists_assets_with_consumption_in_24_months(tenant_a):
    with tenant_context(tenant_a.org):
        a = tenant_a.admin
        part = services.create_part(membership=a, code="ROD", name="Rodamiento")
        services.receive_stock(membership=a, part=part, quantity=10, unit_cost="5")
        for quantity in (1, 2):
            add_part_usage(work_order=make_work_order(tenant_a), membership=a, part=part, quantity=quantity)
        old = add_part_usage(work_order=make_work_order(tenant_a), membership=a, part=part, quantity=4)
        backdate(old.stock_movement, datetime.timedelta(days=800))
        # Una OT sobre una ubicación (sin activo) no cuenta como activo.
        from apps.organizations.models import Location

        site = Location.objects.create(code="S", name="Sitio")
        add_part_usage(
            work_order=make_work_order(tenant_a, location=site), membership=a, part=part, quantity=1
        )
        where = part_where_used(part, membership=a)
    assert len(where.assets) == 1
    use = where.assets[0]
    assert (use.code, use.quantity, where.more_assets) == ("CMP-01", Decimal("3"), False)
    assert use.last_used_at > timezone.now() - datetime.timedelta(days=1)


def test_where_used_after_removing_a_line_counts_net_quantity(tenant_a):
    from apps.work.services import remove_cost_line

    with tenant_context(tenant_a.org):
        a = tenant_a.admin
        part = services.create_part(membership=a, code="ROD", name="Rodamiento")
        wo = make_work_order(tenant_a)
        add_part_usage(work_order=wo, membership=a, part=part, quantity=2)
        line = add_part_usage(work_order=wo, membership=a, part=part, quantity=5)
        remove_cost_line(line=line, membership=a)
        assert not PartUsage.objects.filter(pk=line.pk).exists()
        where = part_where_used(part, membership=a)
    assert [use.quantity for use in where.assets] == [Decimal("2")]


def test_part_detail_shows_where_used(client, tenant_a):
    with tenant_context(tenant_a.org):
        a = tenant_a.admin
        part = services.create_part(membership=a, code="FIL", name="Filtro")
        plan = _plan_with_part(tenant_a, part, code="PMA", asset_class=tenant_a.asset.asset_class)
        add_part_usage(work_order=make_work_order(tenant_a), membership=a, part=part, quantity=1)
    url = reverse("inventory_part_detail", args=[part.pk])
    login(client, tenant_a.admin)
    body = client.get(url).content.decode()
    assert "Dónde se usa" in body
    assert reverse("plan_detail", args=[plan.pk]) in body
    assert reverse("asset_detail", args=[tenant_a.asset.pk]) in body
    # El técnico ve el plan, sin enlace (la ficha del plan es de plan.manage).
    login(client, tenant_a.tech)
    body = client.get(url).content.decode()
    assert "Plan PMA" in body
    assert reverse("plan_detail", args=[plan.pk]) not in body
