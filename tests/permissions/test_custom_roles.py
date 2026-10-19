"""Roles personalizados (ORG-12): conceden y quitan permisos en vistas y servicios; nunca de admin."""

import pytest
from django.urls import reverse

from apps.accounts import services
from apps.accounts.forms import CustomRoleForm
from apps.accounts.models import Membership
from apps.accounts.permissions import (
    ADMIN_ONLY_PERMS,
    CUSTOMIZABLE_PERMS,
    PERM_GROUPS,
    Perm,
)
from apps.core.tenancy import tenant_context
from apps.organizations.models import Plan
from apps.work.models import WorkOrder
from apps.work.services import WorkOrderError, allowed_transitions, create_work_order, transition
from tests.conftest import login

Role = Membership.Role


def _assign(tenant, target, *, name, base_role, permissions):
    with tenant_context(tenant.org):
        role = services.create_custom_role(
            membership=tenant.admin, name=name, base_role=base_role, permissions=permissions
        )
        services.update_member(
            membership=tenant.admin,
            target=target,
            role=base_role,
            custom_role=role,
            restricted_to_locations=False,
            locations=[],
        )
    return role


def test_screen_groups_cover_every_customizable_permission_and_no_admin_one():
    listed = {perm for _key, _label, perms in PERM_GROUPS for perm in perms}
    assert listed == CUSTOMIZABLE_PERMS
    assert not listed & ADMIN_ONLY_PERMS


def test_form_has_no_admin_permission_fields(in_a):
    role = services.create_custom_role(membership=in_a.admin, name="P", base_role=Role.PLANNER)
    form = CustomRoleForm(role=role)
    offered = {
        code for name, field in form.fields.items() if name.startswith("perms_") for code, _ in field.choices
    }
    assert offered == {perm.value for perm in CUSTOMIZABLE_PERMS}
    # Un POST con un permiso de admin no es una opción válida del formulario.
    data = {"name": "P", "perms_work": ["team.manage"]}
    assert not CustomRoleForm(data, role=role).is_valid()


@pytest.mark.parametrize("who", ["tech", "viewer"])
def test_only_admins_see_role_screens(client, tenant_a, who):
    login(client, getattr(tenant_a, who))
    assert client.get(reverse("custom_role_list")).status_code == 403


def test_planner_cannot_manage_roles(client, tenant_a):
    tenant_a.viewer.role = Role.PLANNER
    tenant_a.viewer.save(update_fields=["role"])
    login(client, tenant_a.viewer)
    assert client.get(reverse("custom_role_list")).status_code == 403
    assert client.post(reverse("custom_role_list"), {"name": "X", "base_role": "planner"}).status_code == 403


def test_admin_creates_and_edits_role_from_screens(client, tenant_a):
    login(client, tenant_a.admin)
    assert client.get(reverse("custom_role_list")).status_code == 200
    response = client.post(reverse("custom_role_list"), {"name": "Bodeguero", "base_role": "technician"})
    with tenant_context(tenant_a.org):
        from apps.accounts.models import CustomRole

        role = CustomRole.objects.get(name="Bodeguero")
    assert response.status_code == 302 and response.url == reverse("custom_role_edit", args=[role.pk])
    page = client.get(response.url)
    assert page.status_code == 200
    assert "team.manage" not in page.content.decode()
    response = client.post(
        reverse("custom_role_edit", args=[role.pk]),
        {"name": "Bodeguero", "perms_inventory": ["inventory.view", "inventory.move"], "perms_work": []},
    )
    assert response.status_code == 302
    with tenant_context(tenant_a.org):
        role.refresh_from_db()
    assert role.permissions == ["inventory.move", "inventory.view"]
    assert client.post(reverse("custom_role_active", args=[role.pk]), {"active": "0"}).status_code == 302
    with tenant_context(tenant_a.org):
        role.refresh_from_db()
    assert not role.is_active


def test_custom_role_grants_a_permission_the_base_role_lacks(client, tenant_a):
    """Base solo lectura + gestionar OT: entra a crear OT (el rol base no puede)."""
    login(client, tenant_a.viewer)
    assert client.get(reverse("work_order_create")).status_code == 403
    _assign(
        tenant_a,
        tenant_a.viewer,
        name="Jefe de turno",
        base_role=Role.VIEWER,
        permissions=[Perm.REQUEST_CREATE, Perm.WORK_ORDER_MANAGE],
    )
    assert client.get(reverse("work_order_create")).status_code == 200


def test_custom_role_removes_a_permission_of_the_base_role(client, tenant_a):
    """Base técnico sin ejecutar OT: no puede iniciar una OT asignada."""
    with tenant_context(tenant_a.org):
        work_order = create_work_order(
            work_type="corrective", title="Fuga", asset=tenant_a.asset, assigned_to=tenant_a.tech.user
        )
        # Con su rol base (técnico) sí podría iniciarla.
        assert WorkOrder.Status.IN_PROGRESS in allowed_transitions(
            work_order=work_order, membership=tenant_a.tech
        )
    _assign(
        tenant_a,
        tenant_a.tech,
        name="Pañolero",
        base_role=Role.TECHNICIAN,
        permissions=[Perm.INVENTORY_VIEW, Perm.INVENTORY_MOVE],
    )
    tech = Membership.objects.get(pk=tenant_a.tech.pk)
    with tenant_context(tenant_a.org):
        assert WorkOrder.Status.IN_PROGRESS not in allowed_transitions(work_order=work_order, membership=tech)
        with pytest.raises(WorkOrderError):
            transition(work_order=work_order, to_status=WorkOrder.Status.IN_PROGRESS, membership=tech)
    login(client, tenant_a.tech)
    assert client.get(reverse("work_order_my_week")).status_code == 403
    assert client.get(reverse("inventory_movements")).status_code == 200


def test_without_module_screens_are_blocked_and_member_falls_back(client, tenant_a):
    _assign(
        tenant_a,
        tenant_a.viewer,
        name="Jefe de turno",
        base_role=Role.VIEWER,
        permissions=[Perm.REQUEST_CREATE, Perm.WORK_ORDER_MANAGE],
    )
    tenant_a.org.plan = Plan.objects.create(
        code="basico-x", name="Básico", features=["assets", "work_orders"]
    )
    tenant_a.org.save(update_fields=["plan"])
    login(client, tenant_a.admin)
    response = client.get(reverse("custom_role_list"))
    assert response.status_code == 403
    assert "Roles personalizados" in response.content.decode()
    login(client, tenant_a.viewer)
    assert client.get(reverse("work_order_create")).status_code == 403


def test_role_selector_offers_custom_roles_only_with_module(client, tenant_a):
    with tenant_context(tenant_a.org):
        services.create_custom_role(membership=tenant_a.admin, name="Bodeguero", base_role=Role.TECHNICIAN)
    login(client, tenant_a.admin)
    assert "Bodeguero" in client.get(reverse("team")).content.decode()
    assert "Bodeguero" in client.get(reverse("member_edit", args=[tenant_a.tech.pk])).content.decode()
    tenant_a.org.plan = Plan.objects.create(code="basico-y", name="Básico", features=["assets"])
    tenant_a.org.save(update_fields=["plan"])
    assert "Bodeguero" not in client.get(reverse("team")).content.decode()
