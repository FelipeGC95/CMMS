"""Permisos de repuestos en la OT (INV-16): consumir = work_order.execute, reservar = work_order.manage."""

import pytest
from django.urls import reverse

from apps.accounts.models import Membership
from apps.accounts.permissions import PermissionDeniedError
from apps.core.tenancy import tenant_context
from apps.inventory import services as inventory
from apps.inventory.models import Reservation
from apps.organizations.models import Plan
from apps.organizations.modules import ModuleNotIncludedError
from apps.work.models import PartUsage
from apps.work.services import add_part_usage, remove_cost_line
from tests.conftest import login
from tests.inventory_utils import add_member, make_work_order

pytestmark = pytest.mark.usefixtures("reconciled_inventory")
Role = Membership.Role


@pytest.fixture
def setup(tenant_a):
    with tenant_context(tenant_a.org):
        part = inventory.create_part(membership=tenant_a.admin, code="ROD", name="Rodamiento")
        wo = make_work_order(tenant_a)
        reservation = inventory.reserve_for_work_order(work_order=wo, part=part, quantity=2, source="manual")
        supervisor = add_member(tenant_a, Role.SUPERVISOR)
        external = add_member(tenant_a, Role.EXTERNAL)
    return {"part": part, "wo": wo, "res": reservation, "supervisor": supervisor, "external": external}


def test_only_executors_consume(tenant_a, setup):
    with tenant_context(tenant_a.org):
        for membership in (tenant_a.viewer, setup["external"]):
            with pytest.raises(PermissionDeniedError):
                add_part_usage(work_order=setup["wo"], membership=membership, part=setup["part"], quantity=1)
        usage = add_part_usage(
            work_order=setup["wo"], membership=tenant_a.tech, part=setup["part"], quantity=1
        )
        with pytest.raises(PermissionDeniedError):
            remove_cost_line(line=usage, membership=tenant_a.viewer)
        remove_cost_line(line=usage, membership=tenant_a.tech)


def test_only_managers_release(tenant_a, setup):
    with tenant_context(tenant_a.org):
        with pytest.raises(PermissionDeniedError):
            inventory.release_reservation(membership=tenant_a.tech, reservation=setup["res"])
        inventory.release_reservation(membership=setup["supervisor"], reservation=setup["res"])


@pytest.mark.parametrize(
    ("who", "use", "reserve"),
    [("admin", 302, 302), ("supervisor", 302, 302), ("tech", 302, 403), ("viewer", 403, 403)],
)
def test_views_by_role(client, tenant_a, setup, who, use, reserve):
    membership = setup["supervisor"] if who == "supervisor" else getattr(tenant_a, who)
    login(client, membership)
    wo, part, res = setup["wo"], setup["part"], setup["res"]
    data = {"part": str(part.pk), "quantity": "1"}
    assert client.post(reverse("work_order_part_add", args=[wo.pk]), data).status_code == use
    assert client.post(reverse("inventory_wo_reserve", args=[wo.pk]), data).status_code == reserve
    assert client.post(reverse("inventory_wo_release", args=[wo.pk, res.pk])).status_code == reserve
    with tenant_context(tenant_a.org):
        assert PartUsage.objects.exists() == (use == 302)
        res.refresh_from_db()
        assert (res.status == Reservation.Status.RELEASED) == (reserve == 302)


def test_external_cannot_search_the_catalog_from_a_work_order(client, tenant_a, setup):
    login(client, setup["external"])
    assert client.get(reverse("inventory_wo_part_search", args=[setup["wo"].pk])).status_code in (403, 404)


@pytest.mark.parametrize("who", ["admin", "tech", "viewer"])
def test_without_inventory_module_every_role_gets_403(client, tenant_a, setup, who):
    tenant_a.org.plan = Plan.objects.create(
        code="basico-x", name="Básico", features=["assets", "work_orders"]
    )
    tenant_a.org.save(update_fields=["plan"])
    login(client, getattr(tenant_a, who))
    wo, res = setup["wo"], setup["res"]
    assert client.get(reverse("inventory_wo_part_search", args=[wo.pk])).status_code == 403
    assert client.post(reverse("inventory_wo_reserve", args=[wo.pk]), {}).status_code == 403
    assert client.post(reverse("inventory_wo_release", args=[wo.pk, res.pk])).status_code == 403


def test_service_refuses_catalog_part_without_module_even_for_admin(tenant_a, setup):
    tenant_a.org.plan = Plan.objects.create(
        code="basico-x", name="Básico", features=["assets", "work_orders"]
    )
    tenant_a.org.save(update_fields=["plan"])
    with tenant_context(tenant_a.org):
        wo = setup["wo"]
        wo.organization.refresh_from_db()
        with pytest.raises(ModuleNotIncludedError):
            add_part_usage(work_order=wo, membership=tenant_a.admin, part=setup["part"], quantity=1)
