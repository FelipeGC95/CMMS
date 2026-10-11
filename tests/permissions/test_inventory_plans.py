"""Permisos de los repuestos del catálogo en planes (INV-11, INV-16).

Enlazar en el plan y buscar en el catálogo desde el plan = `plan.manage`;
«Crear catálogo desde los planes» = `inventory.manage`. Ambos: administrador y planificador.
"""

from decimal import Decimal

import pytest
from django.urls import reverse

from apps.accounts.models import Membership
from apps.accounts.permissions import PermissionDeniedError
from apps.core.tenancy import tenant_context
from apps.inventory import services as inventory
from apps.inventory.from_plans import create_parts_from_plan_resources, plan_part_proposals
from apps.maintenance import services as plans
from apps.maintenance.models import PlanResource
from tests.conftest import login
from tests.inventory_utils import add_member

pytestmark = pytest.mark.usefixtures("reconciled_inventory")
Role = Membership.Role
ALLOWED = {Role.ADMIN, Role.PLANNER}


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
        part = inventory.create_part(membership=tenant_a.admin, code="FIL", name="Filtro")
        plan = plans.create_plan(
            code="PX",
            name="PX",
            calendar_interval=30,
            items=[{"text": "x", "item_type": "check"}],
            resources=[{"kind": "part", "description": "Correa", "quantity": Decimal(1), "unit": ""}],
        )
        resource = plan.current_version.resources.get()
    return members, part, plan, resource


@pytest.mark.parametrize("role", list(Role))
def test_services_require_plan_or_inventory_manage(tenant_a, setup, role):
    members, part, _plan, resource = setup
    membership = members[role]
    with tenant_context(tenant_a.org):
        if role in ALLOWED:
            assert plan_part_proposals(membership=membership)
            plans.link_published_resource(membership=membership, resource=resource, part=part)
            assert PlanResource.objects.get(pk=resource.pk).part_id == part.pk
            return
        with pytest.raises(PermissionDeniedError):
            plan_part_proposals(membership=membership)
        with pytest.raises(PermissionDeniedError):
            create_parts_from_plan_resources(membership=membership)
        with pytest.raises(PermissionDeniedError):
            plans.link_published_resource(membership=membership, resource=resource, part=part)
        assert PlanResource.objects.get(pk=resource.pk).part_id is None


@pytest.mark.parametrize("role", list(Role))
def test_screens_require_the_permission(client, tenant_a, setup, role):
    members, part, plan, resource = setup
    login(client, members[role])
    expected = 200 if role in ALLOWED else 403
    assert client.get(reverse("inventory_from_plans")).status_code == expected
    assert client.get(reverse("inventory_plan_part_search"), {"part_q": "fil"}).status_code == expected
    assert client.get(reverse("plan_resource_link", args=[plan.pk, resource.pk])).status_code == expected
    response = client.post(reverse("inventory_from_plans_confirm"))
    assert response.status_code == (302 if role in ALLOWED else 403)
