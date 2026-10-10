"""Permisos de inventario (INV-16): matriz por rol, servicios y módulo de plan."""

import pytest

from apps.accounts.models import Membership
from apps.accounts.permissions import Perm, PermissionDeniedError, has_perm
from apps.core.tenancy import tenant_context
from apps.inventory import services
from apps.inventory.selectors import part_detail, stock_list, visible_warehouses
from apps.notifications.models import Kind
from apps.notifications.selectors import receivable_kinds
from apps.organizations.models import Plan
from apps.organizations.modules import API_MODULES, has_module, module_for_path
from apps.organizations.plans import DEFAULT_PLANS
from tests.conftest import login
from tests.inventory_utils import add_member, make_work_order

pytestmark = pytest.mark.usefixtures("reconciled_inventory")
Role = Membership.Role
P = Perm

# INV-16: rol → (view, cost_view, move, manage)
MATRIX = {
    Role.ADMIN: (True, True, True, True),
    Role.PLANNER: (True, True, True, True),
    Role.SUPERVISOR: (True, True, True, False),
    Role.TECHNICIAN: (True, False, False, False),
    Role.VIEWER: (True, True, False, False),
    Role.EXTERNAL: (False, False, False, False),
}
PERMS = (P.INVENTORY_VIEW, P.INVENTORY_COST_VIEW, P.INVENTORY_MOVE, P.INVENTORY_MANAGE)


@pytest.mark.parametrize("role", list(MATRIX))
def test_inventory_permission_matrix(role):
    membership = Membership(role=role)
    assert tuple(has_perm(membership, perm) for perm in PERMS) == MATRIX[role]


@pytest.fixture
def members(tenant_a):
    return {
        Role.ADMIN: tenant_a.admin,
        Role.PLANNER: add_member(tenant_a, Role.PLANNER),
        Role.SUPERVISOR: add_member(tenant_a, Role.SUPERVISOR),
        Role.TECHNICIAN: tenant_a.tech,
        Role.VIEWER: tenant_a.viewer,
        Role.EXTERNAL: add_member(tenant_a, Role.EXTERNAL),
    }


@pytest.mark.parametrize("role", list(MATRIX))
def test_services_enforce_the_matrix(tenant_a, members, role):
    can_view, _cost, can_move, can_manage = MATRIX[role]
    membership = members[role]
    with tenant_context(tenant_a.org):
        part = services.create_part(membership=tenant_a.admin, code="ROD", name="Rodamiento")
        main = services.ensure_default_warehouse()
        other = services.create_warehouse(membership=tenant_a.admin, code="B2", name="Bodega 2")
        services.receive_stock(membership=tenant_a.admin, part=part, quantity=5)

        def allowed(action):
            try:
                action()
            except PermissionDeniedError:
                return False
            return True

        assert allowed(lambda: list(stock_list(membership=membership))) is can_view
        assert allowed(lambda: list(visible_warehouses(membership=membership))) is can_view
        assert allowed(lambda: part_detail(part.pk, membership=membership)) is can_view
        assert (
            allowed(lambda: services.receive_stock(membership=membership, part=part, quantity=1)) is can_move
        )
        transfer = lambda: services.transfer_stock(  # noqa: E731
            membership=membership, part=part, from_warehouse=main, to_warehouse=other, quantity=1
        )
        assert allowed(transfer) is can_move
        count = lambda: services.count_stock(  # noqa: E731
            membership=membership, part=part, counted_quantity=0, note="baja"
        )
        assert allowed(count) is can_manage
        assert allowed(lambda: services.create_part(membership=membership, code="N", name="N")) is can_manage
        assert allowed(lambda: services.update_part(membership=membership, part=part, name="R")) is can_manage
        assert allowed(
            lambda: services.set_stock_levels(membership=membership, part=part, min_quantity=1)
        ) is (can_manage)
        assert (
            allowed(lambda: services.create_warehouse(membership=membership, code="W", name="W"))
            is can_manage
        )


@pytest.mark.parametrize("role", [Role.ADMIN, Role.SUPERVISOR, Role.TECHNICIAN, Role.VIEWER])
def test_releasing_a_reservation_requires_work_order_manage(tenant_a, members, role):
    with tenant_context(tenant_a.org):
        part = services.create_part(membership=tenant_a.admin, code="ROD", name="Rodamiento")
        wo = make_work_order(tenant_a)
        reservation = services.reserve_for_work_order(work_order=wo, part=part, quantity=1, source="manual")
        if has_perm(members[role], Perm.WORK_ORDER_MANAGE):
            services.release_reservation(membership=members[role], reservation=reservation)
        else:
            with pytest.raises(PermissionDeniedError):
                services.release_reservation(membership=members[role], reservation=reservation)


def test_stock_low_kind_only_for_roles_that_move_stock(tenant_a, members):
    for role, membership in members.items():
        assert (Kind.STOCK_LOW in receivable_kinds(membership=membership)) is MATRIX[role][2]


def test_stock_low_kind_hidden_without_the_module(tenant_a):
    tenant_a.org.plan = Plan.objects.get(code="basic")
    tenant_a.org.save(update_fields=["plan"])
    tenant_a.admin.organization = tenant_a.org
    assert Kind.STOCK_LOW not in receivable_kinds(membership=tenant_a.admin)


# --- Módulo de plan -------------------------------------------------------------


def test_inventory_module_in_trial_pro_and_enterprise_but_not_basic(db):
    extras = {code: extra for code, _name, *_quota, extra in DEFAULT_PLANS}
    assert {code for code, extra in extras.items() if "inventory" in extra} == {"trial", "pro", "enterprise"}
    assert "exports" not in extras["trial"]
    # La migración de datos lo agregó a los planes existentes, sin duplicar.
    for plan in Plan.objects.filter(code__in=["trial", "pro", "enterprise"]):
        assert plan.features.count("inventory") == 1
    assert "inventory" not in Plan.objects.get(code="basic").features


def test_inventory_routes_and_api_belong_to_the_module():
    assert module_for_path("/inventory/") == "inventory"
    assert module_for_path("/inventory/parts/abc/") == "inventory"
    assert module_for_path("/inventory/export.xlsx") == "exports"
    assert module_for_path("/inventory/reorder/export.xlsx") == "exports"
    assert module_for_path("/inventory/movements/export.xlsx") == "exports"
    assert {
        API_MODULES[name] for name in ("api-part", "api-warehouse", "api-stock", "api-stock-movement")
    } == {"inventory"}


@pytest.mark.parametrize("role", ["admin", "tech", "viewer"])
def test_inventory_urls_are_forbidden_without_the_module(client, tenant_a, role):
    tenant_a.org.plan = Plan.objects.get(code="basic")
    tenant_a.org.save(update_fields=["plan"])
    assert not has_module(tenant_a.org, "inventory")
    login(client, getattr(tenant_a, role))
    assert client.get("/inventory/").status_code == 403
    tenant_a.org.plan = Plan.objects.get(code="pro")
    tenant_a.org.save(update_fields=["plan"])
    assert has_module(tenant_a.org, "inventory")
