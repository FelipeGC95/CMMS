"""Planes preventivos: solo `plan.manage` (planificador y administrador); alcance por sitio al asignar."""

import pytest
from django.urls import reverse

from apps.accounts.models import Membership, MembershipLocation, User
from apps.accounts.permissions import PermissionDeniedError
from apps.assets.models import Asset
from apps.core.tenancy import tenant_context
from apps.maintenance import services
from apps.maintenance.models import PlanAssignment
from apps.organizations.models import Location
from tests.conftest import login

READ_URLS = ["plan_list", "plan_create"]
PLAN_URLS = ["plan_detail", "plan_draft", "plan_publish", "plan_discard", "plan_assign"]
PLAN_POSTS = ["plan_draft_start", "plan_assign_class", "plan_publish", "plan_discard"]


@pytest.mark.parametrize("role", ["tech", "viewer"])
def test_non_planners_get_403(client, tenant_a, role):
    login(client, getattr(tenant_a, role))
    for name in READ_URLS:
        assert client.get(reverse(name)).status_code == 403, name
    for name in PLAN_URLS:
        assert client.get(reverse(name, args=[tenant_a.plan.pk])).status_code == 403, name
    for name in PLAN_POSTS:
        assert client.post(reverse(name, args=[tenant_a.plan.pk])).status_code == 403, name
    assert client.get(reverse("asset_plan_assign", args=[tenant_a.asset.pk])).status_code == 403
    with tenant_context(tenant_a.org):
        assert tenant_a.plan.versions.count() == 1
        assert not PlanAssignment.objects.exists()


@pytest.mark.parametrize("role", ["tech", "viewer"])
def test_non_planners_do_not_see_plan_links(client, tenant_a, role):
    login(client, getattr(tenant_a, role))
    assert reverse("plan_list") not in client.get(reverse("menu")).content.decode()
    body = client.get(reverse("asset_detail", args=[tenant_a.asset.pk])).content.decode()
    assert reverse("asset_plan_assign", args=[tenant_a.asset.pk]) not in body


def test_planner_sees_plans_in_menu(client, tenant_a):
    user = User.objects.create_user("p@a.cl", "x")
    planner = Membership.objects.create(user=user, organization=tenant_a.org, role="planner")
    login(client, planner)
    assert reverse("plan_list") in client.get(reverse("menu")).content.decode()
    assert client.get(reverse("plan_list")).status_code == 200


def test_services_require_plan_manage(in_a):
    with pytest.raises(PermissionDeniedError):
        services.create_draft(membership=in_a.tech, plan=in_a.plan)
    with pytest.raises(PermissionDeniedError):
        services.assign_plan_to_assets(membership=in_a.viewer, plan=in_a.plan, targets=[(in_a.asset, None)])


@pytest.fixture
def scoped_planner(tenant_a):
    """Planificador limitado a Planta Norte; hay un compresor en cada planta."""
    user = User.objects.create_user("scoped@a.cl", "x")
    planner = Membership.objects.create(
        user=user, organization=tenant_a.org, role="planner", restricted_to_locations=True
    )
    with tenant_context(tenant_a.org):
        north = Location.objects.create(code="N", name="Planta Norte")
        south = Location.objects.create(code="S", name="Planta Sur")
        cls = tenant_a.asset.asset_class
        inside = Asset.objects.create(code="IN-01", name="Dentro", asset_class=cls, location=north)
        outside = Asset.objects.create(code="OUT-01", name="Fuera", asset_class=cls, location=south)
        MembershipLocation.objects.create(membership=planner, location=north)
    return planner, inside, outside


def test_scoped_planner_picker_lists_only_scope(client, tenant_a, scoped_planner):
    planner, inside, outside = scoped_planner
    login(client, planner)
    url = reverse("plan_assign", args=[tenant_a.plan.pk])
    assert [a.code for a in client.get(url).context["page"].object_list] == ["IN-01"]
    client.post(url, {"assets": [str(inside.pk), str(outside.pk)]})
    with tenant_context(tenant_a.org):
        assert set(PlanAssignment.objects.values_list("asset__code", flat=True)) == {"IN-01"}


def test_scoped_planner_assigns_class_only_within_scope(client, tenant_a, scoped_planner):
    planner, _inside, _outside = scoped_planner
    login(client, planner)
    client.post(reverse("plan_assign_class", args=[tenant_a.plan.pk]))
    with tenant_context(tenant_a.org):
        assert set(PlanAssignment.objects.values_list("asset__code", flat=True)) == {"IN-01"}


def test_scoped_planner_cannot_touch_out_of_scope_assignments(client, tenant_a, scoped_planner):
    planner, _inside, outside = scoped_planner
    with tenant_context(tenant_a.org):
        assignment = services.assign_plan(plan=tenant_a.plan, asset=outside)
    login(client, planner)
    url = reverse("plan_assignment_deactivate", args=[tenant_a.plan.pk, assignment.pk])
    assert client.post(url).status_code == 404
    assert client.get(reverse("asset_plan_assign", args=[outside.pk])).status_code == 404
    with tenant_context(tenant_a.org):
        assert PlanAssignment.objects.get(pk=assignment.pk).is_active
