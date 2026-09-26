"""Permisos y alcance por sitio de OT y solicitudes sobre ubicaciones (WO-01 CA2, REQ-01 CA1, ORG-06 CA2)."""

from dataclasses import dataclass

import pytest
from django.urls import reverse

from apps.accounts.models import Membership, MembershipLocation
from apps.accounts.permissions import PermissionDeniedError
from apps.core.tenancy import tenant_context
from apps.organizations.models import Location
from apps.work.models import WorkOrder, WorkRequest
from apps.work.selectors import visible_requests, visible_work_orders
from apps.work.services import create_work_order, report_failure, set_assignees
from tests.conftest import login


@dataclass
class Scene:
    north_area: Location
    south: Location
    wo_inside: WorkOrder
    wo_outside: WorkOrder


@pytest.fixture
def scene(tenant_a):
    """Planta Norte (con su área) y Planta Sur; el técnico queda limitado a Planta Norte."""
    with tenant_context(tenant_a.org):
        north = Location.objects.create(code="N", name="Planta Norte")
        north_area = Location.objects.create(code="N-A", name="Bodega Norte", kind="area", parent=north)
        south = Location.objects.create(code="S", name="Planta Sur")
        wo_inside = create_work_order(work_type="corrective", title="Dentro", location=north_area)
        wo_outside = create_work_order(work_type="corrective", title="Fuera", location=south)
        MembershipLocation.objects.create(membership=tenant_a.tech, location=north)
    tenant_a.tech.restricted_to_locations = True
    tenant_a.tech.save()
    return Scene(north_area, south, wo_inside, wo_outside)


def test_scoped_member_sees_location_work_orders_only_in_their_sites(client, tenant_a, scene):
    with tenant_context(tenant_a.org):
        visible = set(visible_work_orders(membership=tenant_a.tech).values_list("pk", flat=True))
    assert visible == {scene.wo_inside.pk}  # el área es descendiente del sitio permitido
    login(client, tenant_a.tech)
    listed = client.get(reverse("work_order_list"), {"scope": "open"}).context["page"].object_list
    assert {wo.pk for wo in listed} == {scene.wo_inside.pk}
    assert client.get(reverse("work_order_detail", args=[scene.wo_inside.pk])).status_code == 200
    assert client.get(reverse("work_order_detail", args=[scene.wo_outside.pk])).status_code == 404
    response = client.post(
        reverse("work_order_transition", args=[scene.wo_outside.pk]), {"to": "in_progress"}
    )
    assert response.status_code == 404


def test_scoped_member_reports_only_on_locations_in_scope(client, tenant_a, scene):
    login(client, tenant_a.tech)
    page = client.get(reverse("location_report")).content.decode()
    assert "Bodega Norte" in page and "Planta Sur" not in page
    response = client.post(reverse("location_report"), {"location": str(scene.south.pk), "description": "x"})
    assert response.status_code == 200 and "location" in response.context["form"].errors
    with tenant_context(tenant_a.org):
        assert not WorkRequest.objects.exists()


def test_scoped_planner_sees_location_requests_only_in_their_sites(tenant_a, scene):
    tenant_a.tech.role = Membership.Role.SUPERVISOR
    tenant_a.tech.save()
    with tenant_context(tenant_a.org):
        inside = report_failure(membership=tenant_a.admin, location=scene.north_area, description="a")
        report_failure(membership=tenant_a.admin, location=scene.south, description="b")
        assert set(visible_requests(membership=tenant_a.tech).values_list("pk", flat=True)) == {inside.pk}


@pytest.mark.parametrize("role", ["viewer", "technician", "external"])
def test_any_role_that_can_report_reaches_the_location_form(client, tenant_a, role):
    membership = tenant_a.viewer
    membership.role = role
    membership.save()
    login(client, membership)
    assert client.get(reverse("location_report")).status_code == 200


@pytest.mark.parametrize("who", ["viewer", "tech"])
def test_only_managers_change_assignees(client, tenant_a, who):
    with tenant_context(tenant_a.org):
        wo = create_work_order(work_type="corrective", title="x", asset=tenant_a.asset)
    membership = getattr(tenant_a, who)
    login(client, membership)
    response = client.post(
        reverse("work_order_assignees", args=[wo.pk]), {"helpers": [str(membership.user_id)]}
    )
    assert response.status_code == 403
    detail = client.get(reverse("work_order_detail", args=[wo.pk])).content.decode()
    assert reverse("work_order_assignees", args=[wo.pk]) not in detail
    with tenant_context(tenant_a.org), pytest.raises(PermissionDeniedError):
        set_assignees(work_order=wo, membership=membership, helpers=[membership.user])


def test_create_view_with_a_location_requires_manage(client, tenant_a):
    login(client, tenant_a.tech)
    assert client.get(reverse("work_order_create")).status_code == 403
