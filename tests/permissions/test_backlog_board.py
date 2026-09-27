"""Permisos y alcance por sitio del backlog y la programación semanal (REQ-04/05, ORG-06)."""

import pytest
from django.urls import reverse

from apps.accounts.models import Membership, MembershipLocation, User
from apps.accounts.permissions import PermissionDeniedError
from apps.assets.models import Asset
from apps.core.tenancy import tenant_context
from apps.organizations.models import Location
from apps.work.selectors import backlog
from apps.work.services import WorkOrderError, create_work_order, schedule_work_order
from tests.conftest import PASSWORD, login
from tests.test_backlog_board import next_monday

MANAGE_ONLY = ["work_order_backlog", "work_order_board", "work_order_backlog_export_xlsx"]


def _member(tenant, role):
    user = User.objects.create_user(f"{role}-x@{tenant.org.slug}.cl", PASSWORD)
    return Membership.objects.create(user=user, organization=tenant.org, role=role)


@pytest.mark.parametrize("role", ["admin", "planner", "supervisor"])
def test_managers_open_backlog_and_board(client, tenant_a, role):
    member = tenant_a.admin if role == "admin" else _member(tenant_a, role)
    login(client, member)
    for name in MANAGE_ONLY:
        assert client.get(reverse(name)).status_code == 200, name


@pytest.mark.parametrize("role", ["technician", "viewer", "external"])
def test_others_cannot_open_backlog_or_board(client, tenant_a, role):
    member = {"technician": tenant_a.tech, "viewer": tenant_a.viewer}.get(role) or _member(tenant_a, role)
    login(client, member)
    for name in MANAGE_ONLY:
        assert client.get(reverse(name)).status_code == 403, name


def test_technician_cannot_schedule(client, tenant_a):
    with tenant_context(tenant_a.org):
        wo = create_work_order(work_type="corrective", title="x", asset=tenant_a.asset)
        with pytest.raises(PermissionDeniedError):
            schedule_work_order(work_order=wo, membership=tenant_a.tech, scheduled_for=next_monday())
    login(client, tenant_a.tech)
    url = reverse("work_order_schedule", args=[wo.pk])
    assert client.post(url, {"scheduled_for": next_monday().isoformat()}).status_code == 403
    assert client.post(reverse("work_order_unschedule", args=[wo.pk])).status_code == 403


@pytest.mark.parametrize("role", ["technician", "planner"])
def test_my_week_is_open_to_who_executes(client, tenant_a, role):
    member = tenant_a.tech if role == "technician" else _member(tenant_a, role)
    login(client, member)
    assert client.get(reverse("work_order_my_week")).status_code == 200


def test_viewer_has_no_week(client, tenant_a):
    login(client, tenant_a.viewer)
    assert client.get(reverse("work_order_my_week")).status_code == 403


def test_site_scope_limits_backlog_board_and_scheduling(client, tenant_a):
    with tenant_context(tenant_a.org):
        mine = Location.objects.create(code="P1", name="Planta 1")
        other = Location.objects.create(code="P2", name="Planta 2")
        planner = _member(tenant_a, "planner")
        planner.restricted_to_locations = True
        planner.save()
        MembershipLocation.objects.create(membership=planner, location=mine)
        cls = tenant_a.asset.asset_class
        visible = create_work_order(
            work_type="corrective",
            title="Visible",
            asset=Asset.objects.create(code="V", name="V", asset_class=cls, location=mine),
        )
        hidden = create_work_order(work_type="corrective", title="Oculta", location=other)
        assert list(backlog(membership=planner)) == [visible]
        with pytest.raises(WorkOrderError):
            schedule_work_order(work_order=hidden, membership=planner, scheduled_for=next_monday())
    login(client, planner)
    for name in ("work_order_backlog", "work_order_board"):
        body = client.get(reverse(name)).content.decode()
        assert "Visible" in body and "Oculta" not in body, name
    response = client.post(
        reverse("work_order_schedule", args=[hidden.pk]), {"scheduled_for": next_monday().isoformat()}
    )
    assert response.status_code == 404


def test_plan_without_work_orders_module_blocks_the_screens(client, tenant_a):
    from apps.organizations.models import Plan

    plan = Plan.objects.create(code="solo-activos", name="Solo activos", features=["assets"])
    tenant_a.org.plan = plan
    tenant_a.org.save()
    login(client, tenant_a.admin)
    for name in [*MANAGE_ONLY, "work_order_my_week"]:
        assert client.get(reverse(name)).status_code == 403, name
