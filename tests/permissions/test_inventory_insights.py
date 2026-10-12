"""Permisos de los indicadores de inventario (INV-15, Q3) y de "Dónde se usa" (INV-01 CA4)."""

import pytest
from django.urls import reverse

from apps.accounts.models import Membership
from apps.accounts.permissions import PermissionDeniedError
from apps.core.tenancy import tenant_context
from apps.inventory import services
from apps.inventory.selectors import consumption_12m, inventory_insights, inventory_value, stale_parts
from tests.conftest import login
from tests.inventory_utils import add_member

pytestmark = pytest.mark.usefixtures("reconciled_inventory")
Role = Membership.Role
COST_ROLES = {Role.ADMIN, Role.PLANNER, Role.SUPERVISOR, Role.VIEWER}


@pytest.fixture
def members(tenant_a):
    members = {
        Role.ADMIN: tenant_a.admin,
        Role.PLANNER: add_member(tenant_a, Role.PLANNER),
        Role.SUPERVISOR: add_member(tenant_a, Role.SUPERVISOR),
        Role.TECHNICIAN: tenant_a.tech,
        Role.VIEWER: tenant_a.viewer,
    }
    with tenant_context(tenant_a.org):
        part = services.create_part(membership=tenant_a.admin, code="ROD", name="Rodamiento")
        services.receive_stock(membership=tenant_a.admin, part=part, quantity=3, unit_cost="1234")
    return members


@pytest.mark.parametrize("role", [Role.ADMIN, Role.PLANNER, Role.SUPERVISOR, Role.TECHNICIAN, Role.VIEWER])
def test_only_cost_viewers_see_inventory_insights(client, members, role):
    login(client, members[role])
    body = client.get(reverse("inventory_home")).content.decode()
    shown = "Valor del inventario" in body
    assert shown == (role in COST_ROLES)
    assert ("3.702" in body) == (role in COST_ROLES)  # 3 × 1.234


def test_insight_selectors_require_cost_view(tenant_a, members):
    with tenant_context(tenant_a.org):
        for selector in (inventory_value, consumption_12m, stale_parts, inventory_insights):
            with pytest.raises(PermissionDeniedError):
                selector(membership=tenant_a.tech)


def test_external_cannot_open_inventory(client, tenant_a, members):
    login(client, add_member(tenant_a, Role.EXTERNAL))
    assert client.get(reverse("inventory_home")).status_code == 403
