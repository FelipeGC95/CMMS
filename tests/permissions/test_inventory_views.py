"""Permisos de las pantallas de inventario por rol y módulo de plan (INV-16)."""

import pytest
from django.urls import reverse

from apps.accounts.models import Membership
from apps.core.tenancy import tenant_context
from apps.inventory import services
from apps.organizations.models import Plan
from tests.conftest import login
from tests.inventory_utils import add_member

pytestmark = pytest.mark.usefixtures("reconciled_inventory")
Role = Membership.Role
VIEW_ROLES = {Role.ADMIN, Role.PLANNER, Role.SUPERVISOR, Role.TECHNICIAN, Role.VIEWER}
MANAGE_ROLES = {Role.ADMIN, Role.PLANNER}


@pytest.fixture
def setup(tenant_a):
    members = {
        Role.ADMIN: tenant_a.admin,
        Role.PLANNER: add_member(tenant_a, Role.PLANNER),
        Role.SUPERVISOR: add_member(tenant_a, Role.SUPERVISOR),
        Role.TECHNICIAN: tenant_a.tech,
        Role.VIEWER: tenant_a.viewer,
        Role.EXTERNAL: add_member(tenant_a, Role.EXTERNAL),
    }
    with tenant_context(tenant_a.org):
        part = services.create_part(membership=tenant_a.admin, code="ROD", name="Rodamiento")
        main = services.ensure_default_warehouse()
        spare = services.create_warehouse(membership=tenant_a.admin, code="VAC", name="Vacía")
    return members, part, main, spare


def _view_urls(part):
    return [
        reverse("inventory_home"),
        reverse("inventory_part_detail", args=[part.pk]),
        reverse("inventory_part_search") + "?q=rod",
        reverse("inventory_warehouse_list"),
    ]


def _manage_gets(part, warehouse):
    return [
        reverse("inventory_part_create"),
        reverse("inventory_part_edit", args=[part.pk]),
        reverse("inventory_warehouse_create"),
        reverse("inventory_warehouse_edit", args=[warehouse.pk]),
        reverse("inventory_import"),
        reverse("inventory_import_template"),
    ]


def _manage_posts(part, main, spare):
    return [
        (reverse("inventory_part_active", args=[part.pk]), {"is_active": "1"}),
        (reverse("inventory_part_levels", args=[part.pk]), {"warehouse": main.pk, "min_quantity": "1"}),
        (reverse("inventory_warehouse_default", args=[main.pk]), {}),
        (reverse("inventory_warehouse_active", args=[spare.pk]), {"is_active": "1"}),
        (reverse("inventory_part_create"), {"code": "N", "name": "N"}),
    ]


@pytest.mark.parametrize("role", list(Role))
def test_view_screens_follow_inventory_view(client, setup, role):
    members, part, _main, _spare = setup
    login(client, members[role])
    for url in _view_urls(part):
        assert client.get(url).status_code == (200 if role in VIEW_ROLES else 403), url


@pytest.mark.parametrize("role", list(Role))
def test_management_screens_follow_inventory_manage(client, setup, role):
    members, part, main, spare = setup
    login(client, members[role])
    for url in _manage_gets(part, main):
        assert client.get(url).status_code == (200 if role in MANAGE_ROLES else 403), url
    for url, data in _manage_posts(part, main, spare):
        status = client.post(url, data).status_code
        assert (status in (200, 302)) is (role in MANAGE_ROLES), (url, status)
        if role not in MANAGE_ROLES:
            assert status == 403, url


@pytest.mark.parametrize("role", [Role.SUPERVISOR, Role.TECHNICIAN, Role.VIEWER])
def test_read_only_roles_see_no_management_actions(client, setup, role):
    members, part, _main, _spare = setup
    login(client, members[role])
    home = client.get(reverse("inventory_home")).content.decode()
    detail = client.get(reverse("inventory_part_detail", args=[part.pk])).content.decode()
    for url in (reverse("inventory_part_create"), reverse("inventory_import")):
        assert url not in home
    assert reverse("inventory_part_edit", args=[part.pk]) not in detail
    assert reverse("inventory_part_levels", args=[part.pk]) not in detail
    warehouses = client.get(reverse("inventory_warehouse_list")).content.decode()
    assert reverse("inventory_warehouse_create") not in warehouses


def test_cost_columns_only_with_cost_view(client, setup):
    members, part, _main, _spare = setup
    for role, sees in ((Role.VIEWER, True), (Role.SUPERVISOR, True), (Role.TECHNICIAN, False)):
        login(client, members[role])
        detail = client.get(reverse("inventory_part_detail", args=[part.pk])).content.decode()
        assert ("Costo promedio" in detail) is sees, role


@pytest.mark.parametrize("role", [Role.ADMIN, Role.TECHNICIAN])
def test_every_inventory_url_is_forbidden_without_the_module(client, tenant_a, setup, role):
    members, part, main, spare = setup
    tenant_a.org.plan = Plan.objects.get(code="basic")
    tenant_a.org.save(update_fields=["plan"])
    login(client, members[role])
    for url in [*_view_urls(part), *_manage_gets(part, main)]:
        assert client.get(url).status_code == 403, url
    for url, data in _manage_posts(part, main, spare):
        assert client.post(url, data).status_code == 403, url
