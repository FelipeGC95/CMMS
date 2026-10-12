"""Permisos de entrada, conteo, transferencia, movimientos, reposición y Excel de inventario por rol
y por módulo de plan (INV-16): move para entrada y transferencia, manage para conteo, view para
las listas y el Excel (que además pide el módulo `exports`)."""

import uuid

import pytest
from django.urls import reverse

from apps.accounts.models import Membership
from apps.core.tenancy import tenant_context
from apps.inventory import services
from apps.inventory.models import StockMovement
from apps.organizations.models import Plan
from tests.conftest import login
from tests.inventory_utils import add_member

pytestmark = pytest.mark.usefixtures("reconciled_inventory")
Role = Membership.Role
VIEW_ROLES = {Role.ADMIN, Role.PLANNER, Role.SUPERVISOR, Role.TECHNICIAN, Role.VIEWER}
MOVE_ROLES = {Role.ADMIN, Role.PLANNER, Role.SUPERVISOR}
MANAGE_ROLES = {Role.ADMIN, Role.PLANNER}
EXCEL = ("inventory_export_xlsx", "inventory_movements_export_xlsx", "inventory_reorder_export_xlsx")


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
        spare = services.create_warehouse(membership=tenant_a.admin, code="SEG", name="Segunda")
        services.receive_stock(membership=tenant_a.admin, part=part, quantity=10, unit_cost="5")
    return members, part, main, spare


def _move_requests(part, main, spare):
    return [
        ("get", reverse("inventory_receive"), {}),
        ("post", reverse("inventory_receive"), {"part": part.pk, "quantity": "1", "warehouse": main.pk}),
        ("get", reverse("inventory_transfer"), {}),
        (
            "post",
            reverse("inventory_transfer"),
            {"part": part.pk, "from_warehouse": main.pk, "to_warehouse": spare.pk, "quantity": "1"},
        ),
    ]


def _manage_requests(part, main):
    url = reverse("inventory_part_count", args=[part.pk])
    return [
        ("get", reverse("inventory_count"), {}),
        ("get", url, {}),
        ("post", url, {"counted_quantity": "10", "warehouse": main.pk}),
    ]


def _send(client, method, url, data):
    if method == "post":
        return client.post(url, {**data, "client_uuid": uuid.uuid4()})
    return client.get(url, data)


@pytest.mark.parametrize("role", list(Role))
def test_receive_and_transfer_follow_inventory_move(client, tenant_a, setup, role):
    members, part, main, spare = setup
    login(client, members[role])
    for method, url, data in _move_requests(part, main, spare):
        status = _send(client, method, url, data).status_code
        assert status == ({"get": 200, "post": 302}[method] if role in MOVE_ROLES else 403), (url, method)
    with tenant_context(tenant_a.org):
        assert StockMovement.objects.count() == (4 if role in MOVE_ROLES else 1)


@pytest.mark.parametrize("role", list(Role))
def test_count_follows_inventory_manage(client, setup, role):
    members, part, main, _spare = setup
    login(client, members[role])
    for method, url, data in _manage_requests(part, main):
        status = _send(client, method, url, data).status_code
        assert status == ({"get": 200, "post": 302}[method] if role in MANAGE_ROLES else 403), (url, method)


@pytest.mark.parametrize("role", list(Role))
def test_lists_and_excel_follow_inventory_view(client, setup, role):
    members, _part, _main, _spare = setup
    login(client, members[role])
    for name in ("inventory_movements", "inventory_reorder", *EXCEL):
        assert client.get(reverse(name)).status_code == (200 if role in VIEW_ROLES else 403), name


@pytest.mark.parametrize("role", [Role.SUPERVISOR, Role.TECHNICIAN, Role.VIEWER])
def test_buttons_follow_the_role(client, setup, role):
    members, part, _main, _spare = setup
    login(client, members[role])
    home = client.get(reverse("inventory_home")).content.decode()
    detail = client.get(reverse("inventory_part_detail", args=[part.pk])).content.decode()
    reorder = client.get(reverse("inventory_reorder")).content.decode()
    can_move = role in MOVE_ROLES
    assert (reverse("inventory_receive") in home) is can_move
    assert (reverse("inventory_transfer") in home) is can_move
    assert (reverse("inventory_receive") in reorder) is can_move
    assert (reverse("inventory_transfer") in detail) is can_move
    assert reverse("inventory_count") not in home
    assert reverse("inventory_part_count", args=[part.pk]) not in detail


@pytest.mark.parametrize("role", list(Role))
def test_more_menu_links_follow_inventory_view(client, setup, role):
    members, *_rest = setup
    login(client, members[role])
    menu = client.get(reverse("menu")).content.decode()
    for name in ("inventory_movements", "inventory_reorder"):
        assert (reverse(name) in menu) is (role in VIEW_ROLES), name


@pytest.mark.parametrize("role", [Role.ADMIN, Role.TECHNICIAN])
def test_everything_is_forbidden_without_the_inventory_module(client, tenant_a, setup, role):
    members, part, main, spare = setup
    tenant_a.org.plan = Plan.objects.get(code="basic")
    tenant_a.org.save(update_fields=["plan"])
    login(client, members[role])
    requests = [*_move_requests(part, main, spare), *_manage_requests(part, main)]
    requests += [("get", reverse(name), {}) for name in ("inventory_movements", "inventory_reorder", *EXCEL)]
    for method, url, data in requests:
        assert _send(client, method, url, data).status_code == 403, (url, method)
    menu = client.get(reverse("menu")).content.decode()
    assert reverse("inventory_movements") not in menu and reverse("inventory_reorder") not in menu


def test_excel_without_the_exports_module_is_forbidden_and_hidden(client, tenant_a, setup):
    members, *_rest = setup
    tenant_a.org.plan = Plan.objects.get(code="trial")
    tenant_a.org.save(update_fields=["plan"])
    login(client, members[Role.ADMIN])
    for name in EXCEL:
        assert client.get(reverse(name)).status_code == 403, name
    for name in ("inventory_home", "inventory_movements", "inventory_reorder"):
        body = client.get(reverse(name)).content.decode()
        assert not any(reverse(excel) in body for excel in EXCEL), name
