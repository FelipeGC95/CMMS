"""Roles personalizados (ORG-12, ADR-031): servicios con base de datos real."""

import pytest

from apps.accounts import services
from apps.accounts.models import CustomRole, Invitation, Membership, User
from apps.accounts.permissions import (
    ADMIN_ONLY_PERMS,
    ROLE_PERMISSIONS,
    Perm,
    effective_permissions,
    has_perm,
)
from apps.accounts.services import TeamError
from apps.audit.models import AuditEvent
from apps.core.tenancy import tenant_context
from apps.organizations.models import Plan
from apps.organizations.modules import ModuleNotIncludedError
from apps.work.selectors import assignable_members

Role = Membership.Role


def _member(tenant, email, role):
    user = User.objects.create_user(email, "x")
    return Membership.objects.create(user=user, organization=tenant.org, role=role)


def _drop_module(org):
    org.plan = Plan.objects.create(code="sin-roles", name="Sin roles", features=["assets", "work_orders"])
    org.save(update_fields=["plan"])


def _reload(membership):
    return Membership.objects.get(pk=membership.pk)


def test_create_starts_with_base_role_permissions_and_is_audited(in_a):
    role = services.create_custom_role(membership=in_a.admin, name="  Bodeguero ", base_role=Role.TECHNICIAN)
    assert role.name == "Bodeguero"
    assert role.base_role == Role.TECHNICIAN
    assert role.permission_set == ROLE_PERMISSIONS[Role.TECHNICIAN]
    event = AuditEvent.objects.get(target_id=role.pk)
    assert event.action == AuditEvent.Action.CREATE
    assert event.changes["base_role"] == Role.TECHNICIAN


def test_create_with_chosen_permissions(in_a):
    role = services.create_custom_role(
        membership=in_a.admin,
        name="Lector de inventario",
        base_role=Role.VIEWER,
        permissions=[Perm.INVENTORY_VIEW, "request.create"],
    )
    assert role.permissions == ["inventory.view", "request.create"]


@pytest.mark.parametrize("perm", sorted(ADMIN_ONLY_PERMS))
def test_admin_permissions_can_never_be_granted(in_a, perm):
    with pytest.raises(TeamError):
        services.create_custom_role(
            membership=in_a.admin, name="Casi admin", base_role=Role.PLANNER, permissions=[perm]
        )
    role = services.create_custom_role(membership=in_a.admin, name="Planif", base_role=Role.PLANNER)
    with pytest.raises(TeamError):
        services.update_custom_role(
            membership=in_a.admin, role=role, name="Planif", permissions=[*role.permissions, perm]
        )
    assert CustomRole.objects.get(pk=role.pk).permission_set == ROLE_PERMISSIONS[Role.PLANNER]


def test_admin_permissions_stored_by_hand_are_ignored(in_a):
    """Aunque alguien escriba el JSON directo en la base, has_perm no los concede."""
    role = services.create_custom_role(membership=in_a.admin, name="X", base_role=Role.VIEWER)
    CustomRole.objects.filter(pk=role.pk).update(permissions=["team.manage", "audit.view", "request.create"])
    services.update_member(
        membership=in_a.admin,
        target=in_a.viewer,
        role=Role.VIEWER,
        custom_role=role,
        restricted_to_locations=False,
        locations=[],
    )
    viewer = _reload(in_a.viewer)
    assert not has_perm(viewer, Perm.TEAM_MANAGE)
    assert not has_perm(viewer, Perm.AUDIT_VIEW)
    assert has_perm(viewer, Perm.REQUEST_CREATE)


def test_invalid_input_is_rejected(in_a):
    with pytest.raises(TeamError):
        services.create_custom_role(membership=in_a.admin, name="A", base_role=Role.ADMIN)
    with pytest.raises(TeamError):
        services.create_custom_role(membership=in_a.admin, name="A", base_role="owner")
    with pytest.raises(TeamError):
        services.create_custom_role(membership=in_a.admin, name="   ", base_role=Role.VIEWER)
    with pytest.raises(TeamError):
        services.create_custom_role(
            membership=in_a.admin, name="A", base_role=Role.VIEWER, permissions=["asset.delete_all"]
        )
    services.create_custom_role(membership=in_a.admin, name="Bodeguero", base_role=Role.VIEWER)
    with pytest.raises(TeamError):
        services.create_custom_role(membership=in_a.admin, name="BODEGUERO", base_role=Role.VIEWER)


def test_only_admins_manage_roles(in_a):
    from apps.accounts.permissions import PermissionDeniedError

    with pytest.raises(PermissionDeniedError):
        services.create_custom_role(membership=in_a.tech, name="Yo mismo", base_role=Role.PLANNER)


def test_update_changes_name_and_permissions_but_not_base(in_a):
    role = services.create_custom_role(membership=in_a.admin, name="Bodega", base_role=Role.TECHNICIAN)
    role = services.update_custom_role(
        membership=in_a.admin, role=role, name="Bodeguero", permissions=[Perm.INVENTORY_VIEW]
    )
    assert (role.name, role.base_role, role.permissions) == ("Bodeguero", Role.TECHNICIAN, ["inventory.view"])
    event = AuditEvent.objects.filter(target_id=role.pk, action=AuditEvent.Action.UPDATE).get()
    assert event.changes["name"] == ["Bodega", "Bodeguero"]


def test_assigning_sets_base_role_and_grants_and_removes(in_a):
    """CA4: un rol personalizado concede y quita permisos respecto de su rol base."""
    role = services.create_custom_role(
        membership=in_a.admin,
        name="Bodeguero",
        base_role=Role.TECHNICIAN,
        permissions=[Perm.INVENTORY_VIEW, Perm.INVENTORY_MOVE, Perm.REQUEST_CREATE],
    )
    services.update_member(
        membership=in_a.admin,
        target=in_a.viewer,
        role=Role.VIEWER,  # se ignora: manda el rol base del personalizado
        custom_role=role,
        restricted_to_locations=False,
        locations=[],
    )
    member = _reload(in_a.viewer)
    assert (member.role, member.custom_role_id) == (Role.TECHNICIAN, role.pk)
    assert has_perm(member, Perm.INVENTORY_MOVE)  # agregado
    assert not has_perm(member, Perm.WORK_ORDER_EXECUTE)  # quitado
    assert member.role_label == "Bodeguero"
    event = AuditEvent.objects.filter(target_id=member.pk, action=AuditEvent.Action.ROLE_CHANGE).get()
    assert event.changes["custom_role"] == [None, "Bodeguero"]

    # Volver a un rol base limpia el personalizado.
    services.update_member(
        membership=in_a.admin, target=member, role=Role.VIEWER, restricted_to_locations=False, locations=[]
    )
    member = _reload(member)
    assert member.custom_role_id is None
    assert effective_permissions(member) == ROLE_PERMISSIONS[Role.VIEWER]


def test_last_admin_cannot_move_to_a_custom_role(in_a):
    role = services.create_custom_role(membership=in_a.admin, name="Planif", base_role=Role.PLANNER)
    with pytest.raises(TeamError):
        services.update_member(
            membership=in_a.admin,
            target=in_a.admin,
            role=Role.ADMIN,
            custom_role=role,
            restricted_to_locations=False,
            locations=[],
        )
    assert _reload(in_a.admin).role == Role.ADMIN


def test_external_base_keeps_mandatory_scope(in_a):
    role = services.create_custom_role(
        membership=in_a.admin,
        name="Contratista técnico",
        base_role=Role.EXTERNAL,
        permissions=[Perm.REQUEST_CREATE, Perm.WORK_ORDER_EXECUTE],
    )
    with pytest.raises(TeamError):
        services.update_member(
            membership=in_a.admin,
            target=in_a.viewer,
            role=Role.VIEWER,
            custom_role=role,
            restricted_to_locations=False,
            locations=[],
        )


def test_inactive_role_cannot_be_assigned_and_stops_applying(in_a):
    role = services.create_custom_role(membership=in_a.admin, name="Temporal", base_role=Role.VIEWER)
    services.set_custom_role_active(membership=in_a.admin, role=role, active=False)
    with pytest.raises(TeamError):
        services.update_member(
            membership=in_a.admin,
            target=in_a.viewer,
            role=Role.VIEWER,
            custom_role=role,
            restricted_to_locations=False,
            locations=[],
        )
    # Inactivo con una membresía (datos antiguos): rige el rol base.
    Membership.objects.filter(pk=in_a.tech.pk).update(custom_role=role)
    assert effective_permissions(_reload(in_a.tech)) == ROLE_PERMISSIONS[Role.TECHNICIAN]


def test_cannot_deactivate_while_in_use(in_a):
    role = services.create_custom_role(membership=in_a.admin, name="Bodeguero", base_role=Role.TECHNICIAN)
    services.update_member(
        membership=in_a.admin,
        target=in_a.tech,
        role=Role.TECHNICIAN,
        custom_role=role,
        restricted_to_locations=False,
        locations=[],
    )
    with pytest.raises(TeamError, match="1 personas"):
        services.set_custom_role_active(membership=in_a.admin, role=role, active=False)
    services.set_member_active(membership=in_a.admin, target=_reload(in_a.tech), active=False)
    role = services.set_custom_role_active(membership=in_a.admin, role=role, active=False)
    assert not role.is_active
    assert AuditEvent.objects.filter(target_id=role.pk, action=AuditEvent.Action.DEACTIVATE).exists()
    role = services.set_custom_role_active(membership=in_a.admin, role=role, active=True)
    assert role.is_active


def test_cannot_deactivate_with_pending_invitation(in_a):
    role = services.create_custom_role(membership=in_a.admin, name="Bodeguero", base_role=Role.TECHNICIAN)
    services.invite_member(
        membership=in_a.admin,
        email="nuevo@empresa-a.cl",
        role=Role.VIEWER,
        custom_role=role,
        base_url="https://x.cl/",
    )
    with pytest.raises(TeamError, match="1 invitaciones"):
        services.set_custom_role_active(membership=in_a.admin, role=role, active=False)


def test_invitation_with_custom_role(in_a, tenant_a):
    role = services.create_custom_role(
        membership=in_a.admin,
        name="Bodeguero",
        base_role=Role.TECHNICIAN,
        permissions=[*ROLE_PERMISSIONS[Role.TECHNICIAN], Perm.INVENTORY_MOVE],
    )
    invitation = services.invite_member(
        membership=in_a.admin,
        email="nuevo@empresa-a.cl",
        role=Role.VIEWER,
        custom_role=role,
        base_url="https://x.cl/",
    )
    assert (invitation.role, invitation.custom_role_id) == (Role.TECHNICIAN, role.pk)
    assert invitation.role_label == "Bodeguero"
    invitation = Invitation.objects.get(pk=invitation.pk)
    user = User.objects.create_user("nuevo@empresa-a.cl", "x")
    membership = services.accept_invitation(invitation=invitation, user=user)
    assert (membership.role, membership.custom_role_id) == (Role.TECHNICIAN, role.pk)
    assert has_perm(_reload(membership), Perm.INVENTORY_MOVE)


def test_without_module_falls_back_to_base_role_without_deleting(in_a):
    """CA6: si el plan pierde el módulo, rige el rol base y nada se borra."""
    role = services.create_custom_role(
        membership=in_a.admin, name="Bodeguero", base_role=Role.TECHNICIAN, permissions=[Perm.INVENTORY_MOVE]
    )
    services.update_member(
        membership=in_a.admin,
        target=in_a.tech,
        role=Role.TECHNICIAN,
        custom_role=role,
        restricted_to_locations=False,
        locations=[],
    )
    _drop_module(in_a.org)
    tech = Membership.objects.select_related("organization__plan").get(pk=in_a.tech.pk)
    assert effective_permissions(tech) == ROLE_PERMISSIONS[Role.TECHNICIAN]
    assert tech.role_label == "Técnico"
    admin = Membership.objects.select_related("organization__plan").get(pk=in_a.admin.pk)
    with pytest.raises(ModuleNotIncludedError):
        services.create_custom_role(membership=admin, name="Otro", base_role=Role.VIEWER)
    with pytest.raises(ModuleNotIncludedError):
        services.update_member(
            membership=admin,
            target=tech,
            role=Role.TECHNICIAN,
            custom_role=role,
            restricted_to_locations=False,
            locations=[],
        )
    # Editar otra cosa (horas) sin el módulo conserva el rol personalizado guardado.
    services.update_member(
        membership=admin,
        target=tech,
        role=Role.TECHNICIAN,
        restricted_to_locations=False,
        locations=[],
        weekly_capacity_hours=30,
    )
    assert _reload(tech).custom_role_id == role.pk
    assert CustomRole.objects.filter(pk=role.pk, is_active=True).exists()


def test_invitation_accepted_without_module_uses_base_role(in_a):
    role = services.create_custom_role(membership=in_a.admin, name="Bodeguero", base_role=Role.TECHNICIAN)
    invitation = services.invite_member(
        membership=in_a.admin,
        email="nuevo@empresa-a.cl",
        role=Role.VIEWER,
        custom_role=role,
        base_url="https://x.cl/",
    )
    _drop_module(in_a.org)
    user = User.objects.create_user("nuevo@empresa-a.cl", "x")
    invitation = Invitation.objects.select_related("organization__plan").get(pk=invitation.pk)
    membership = services.accept_invitation(invitation=invitation, user=user)
    assert (membership.role, membership.custom_role_id) == (Role.TECHNICIAN, None)


def test_assignable_members_follow_effective_permission(in_a, tenant_a):
    """Responsables de OT: quien ejecuta OT por su rol vigente, base o personalizado."""
    executes = services.create_custom_role(
        membership=in_a.admin,
        name="Operador mantenedor",
        base_role=Role.VIEWER,
        permissions=[Perm.REQUEST_CREATE, Perm.WORK_ORDER_EXECUTE],
    )
    no_execute = services.create_custom_role(
        membership=in_a.admin, name="Pañolero", base_role=Role.TECHNICIAN, permissions=[Perm.INVENTORY_MOVE]
    )
    for target, role in ((in_a.viewer, executes), (in_a.tech, no_execute)):
        services.update_member(
            membership=in_a.admin,
            target=target,
            role=role.base_role,
            custom_role=role,
            restricted_to_locations=False,
            locations=[],
        )
    users = set(assignable_members(membership=in_a.admin))
    assert in_a.viewer.user in users
    assert in_a.tech.user not in users
    assert in_a.admin.user in users


def test_membership_cannot_point_to_role_of_other_organization(in_a, tenant_b):
    from apps.core.tenancy import CrossTenantError

    role = services.create_custom_role(membership=in_a.admin, name="Bodeguero", base_role=Role.TECHNICIAN)
    tenant_b.tech.custom_role_id = role.pk
    with pytest.raises(CrossTenantError):
        tenant_b.tech.save()


def test_seed_demo_has_a_custom_role(db):
    from django.core.management import call_command

    from apps.organizations.models import Organization

    call_command("seed_demo", "--reset", verbosity=0)
    org = Organization.objects.get(slug="alimentos-del-sur")
    with tenant_context(org):
        member = Membership.objects.select_related("custom_role").get(
            organization=org, user__email="tecnico2@demo.cl"
        )
        assert member.role_label == "Bodeguero"
        assert has_perm(member, Perm.INVENTORY_MOVE)
        assert has_perm(member, Perm.WORK_ORDER_EXECUTE)
        assert not has_perm(member, Perm.TEAM_MANAGE)
