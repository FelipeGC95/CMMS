"""Permisos por rol (ADR-013): cada permiso sensible se prueba en el servicio y en la vista."""

import pytest
from django.urls import reverse

from apps.accounts.models import Membership
from apps.accounts.permissions import ROLE_PERMISSIONS, Perm, has_perm
from apps.core.tenancy import tenant_context
from apps.work.models import WorkOrder
from apps.work.services import create_work_order
from tests.conftest import login

Role = Membership.Role


def test_every_role_has_a_permission_set():
    assert set(ROLE_PERMISSIONS) == set(Role.values)


@pytest.mark.parametrize(
    "role,perm,expected",
    [
        (Role.ADMIN, Perm.WORK_ORDER_MANAGE, True),
        (Role.PLANNER, Perm.PLAN_MANAGE, True),
        (Role.SUPERVISOR, Perm.WORK_ORDER_MANAGE, True),
        (Role.SUPERVISOR, Perm.ASSET_MANAGE, False),
        (Role.TECHNICIAN, Perm.WORK_ORDER_EXECUTE, True),
        (Role.TECHNICIAN, Perm.WORK_ORDER_MANAGE, False),
        (Role.VIEWER, Perm.REQUEST_CREATE, True),
        (Role.VIEWER, Perm.WORK_ORDER_EXECUTE, False),
        (Role.EXTERNAL, Perm.WORK_ORDER_EXECUTE, False),
        (Role.ADMIN, Perm.TEAM_MANAGE, True),
        (Role.PLANNER, Perm.TEAM_MANAGE, False),
        (Role.SUPERVISOR, Perm.TEAM_MANAGE, False),
        (Role.ADMIN, Perm.AUDIT_VIEW, True),
        (Role.PLANNER, Perm.AUDIT_VIEW, False),
        (Role.SUPERVISOR, Perm.AUDIT_VIEW, False),
        (Role.TECHNICIAN, Perm.AUDIT_VIEW, False),
        (Role.VIEWER, Perm.AUDIT_VIEW, False),
        (Role.EXTERNAL, Perm.AUDIT_VIEW, False),
    ],
)
def test_role_matrix(role, perm, expected):
    assert has_perm(Membership(role=role), perm) is expected


def test_no_membership_has_no_permissions():
    assert not has_perm(None, Perm.REQUEST_CREATE)


def _work_order(tenant):
    with tenant_context(tenant.org):
        return create_work_order(
            work_type="preventive", title="x", asset=tenant.asset, checklist=tenant.plan.items.all()
        )


def test_anonymous_is_redirected_to_login(client, tenant_a):
    for url in [reverse("dashboard"), reverse("asset_list"), reverse("work_order_list")]:
        response = client.get(url)
        assert response.status_code == 302 and "/accounts/login/" in response["Location"]


def test_viewer_cannot_execute_checklist_via_view(client, tenant_a):
    wo = _work_order(tenant_a)
    with tenant_context(tenant_a.org):
        item = wo.checklist.first()
    login(client, tenant_a.viewer)
    response = client.post(reverse("checklist_answer", args=[wo.pk, item.pk]), {"result": "ok"})
    assert response.status_code == 403


def test_viewer_cannot_complete_via_view(client, tenant_a):
    wo = _work_order(tenant_a)
    login(client, tenant_a.viewer)
    response = client.post(reverse("work_order_complete", args=[wo.pk]), {"labor_minutes": 10})
    assert response.status_code == 403


def test_technician_cannot_cancel_via_view(client, tenant_a):
    wo = _work_order(tenant_a)
    login(client, tenant_a.tech)
    client.post(reverse("work_order_transition", args=[wo.pk]), {"to": "cancelled"})
    with tenant_context(tenant_a.org):
        assert WorkOrder.objects.get(pk=wo.pk).status == "planned"


def test_admin_can_cancel_via_view(client, tenant_a):
    wo = _work_order(tenant_a)
    login(client, tenant_a.admin)
    client.post(reverse("work_order_transition", args=[wo.pk]), {"to": "cancelled"})
    with tenant_context(tenant_a.org):
        assert WorkOrder.objects.get(pk=wo.pk).status == "cancelled"


def test_detail_hides_actions_the_role_cannot_do(client, tenant_a):
    wo = _work_order(tenant_a)
    login(client, tenant_a.viewer)
    context = client.get(reverse("work_order_detail", args=[wo.pk])).context
    assert context["transitions"] == []
    assert context["can_execute"] is False


def test_django_admin_is_scoped_to_active_organization(client, tenant_a, tenant_b):
    from django.contrib.auth.models import Permission

    user = tenant_a.admin.user
    user.is_staff = True
    user.save()
    user.user_permissions.set(Permission.objects.filter(content_type__app_label="assets"))
    login(client, tenant_a.admin)
    listing = client.get(reverse("admin:assets_asset_changelist"))
    assert list(listing.context["cl"].queryset) == [tenant_a.asset]
    assert client.get(reverse("admin:assets_asset_change", args=[tenant_b.asset.pk])).status_code == 302


@pytest.mark.parametrize("role", ["supervisor", "technician", "viewer", "external"])
def test_only_asset_managers_configure_locations_and_classes(client, tenant_a, role):
    member = tenant_a.tech
    member.role = role
    member.save()
    login(client, member)
    for url in [
        reverse("location_list"),
        reverse("asset_class_list"),
        reverse("asset_class_detail", args=[tenant_a.asset.asset_class_id]),
        reverse("asset_attributes", args=[tenant_a.asset.pk]),
    ]:
        assert client.get(url).status_code == 403, url
    assert (
        client.post(reverse("location_list"), {"code": "P", "name": "P", "kind": "site"}).status_code == 403
    )


@pytest.mark.parametrize("role", Role.values)
def test_only_admin_manages_the_organization(role):
    assert has_perm(Membership(role=role), Perm.ORGANIZATION_MANAGE) is (role == Role.ADMIN)


@pytest.mark.parametrize("role", ["planner", "supervisor", "technician", "viewer", "external"])
def test_only_admin_opens_organization_settings(client, tenant_a, role):
    member = tenant_a.tech
    member.role = role
    member.save()
    login(client, member)
    url = reverse("organization_settings")
    assert client.get(url).status_code == 403
    assert client.post(url, {"require_close_approval": "on"}).status_code == 403
    tenant_a.org.refresh_from_db()
    assert tenant_a.org.require_close_approval is False
    assert url not in client.get(reverse("menu")).content.decode()
