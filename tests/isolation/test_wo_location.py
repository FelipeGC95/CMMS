"""Aislamiento de OT y solicitudes sobre ubicaciones y de los responsables de apoyo (WO-01 CA2, REQ-01 CA1).

La organización B no puede apuntar a una ubicación de A, asignar personas de A
ni ver las OT sobre ubicaciones de A.
"""

import pytest
from django.urls import reverse

from apps.core.tenancy import CrossTenantError, tenant_context
from apps.organizations.models import Location
from apps.work.models import WorkOrder, WorkRequest
from apps.work.services import WorkOrderError, create_work_order, report_failure, set_assignees
from tests.conftest import login


@pytest.fixture
def area_a(tenant_a):
    with tenant_context(tenant_a.org):
        return Location.objects.create(code="BOD", name="Bodega A")


@pytest.fixture
def wo_a(tenant_a, area_a):
    with tenant_context(tenant_a.org):
        return create_work_order(work_type="corrective", title="Gotera A", location=area_a)


def test_cannot_create_a_work_order_on_a_foreign_location(tenant_a, tenant_b, area_a):
    with tenant_context(tenant_b.org), pytest.raises(CrossTenantError):
        create_work_order(work_type="corrective", title="x", location=area_a)


def test_cannot_report_a_problem_on_a_foreign_location(tenant_a, tenant_b, area_a):
    with tenant_context(tenant_b.org):
        with pytest.raises(CrossTenantError):
            report_failure(membership=tenant_b.viewer, location=area_a, description="x")
        assert not WorkRequest.objects.exists()


@pytest.mark.parametrize("field", ["assigned_to", "helpers"])
def test_cannot_assign_people_from_another_organization(tenant_a, tenant_b, field):
    foreign_user = tenant_a.tech.user
    value = [foreign_user] if field == "helpers" else foreign_user
    with tenant_context(tenant_b.org):
        with pytest.raises(WorkOrderError):
            create_work_order(work_type="corrective", title="x", asset=tenant_b.asset, **{field: value})
        assert not WorkOrder.objects.exists()


def test_cannot_set_assignees_from_another_organization(tenant_a, tenant_b):
    with tenant_context(tenant_b.org):
        wo = create_work_order(work_type="corrective", title="x", asset=tenant_b.asset)
        with pytest.raises(WorkOrderError):
            set_assignees(work_order=wo, membership=tenant_b.admin, helpers=[tenant_a.tech.user])
        with pytest.raises(WorkOrderError):
            set_assignees(work_order=wo, membership=tenant_b.admin, assigned_to=tenant_a.tech.user)
        assert not wo.helpers.exists()


def test_location_work_orders_of_a_are_invisible_to_b(client, tenant_a, tenant_b, wo_a):
    login(client, tenant_b.admin)
    listed = client.get(reverse("work_order_list"), {"scope": "open"}).context["page"].object_list
    assert wo_a.pk not in {wo.pk for wo in listed}
    assert client.get(reverse("work_order_detail", args=[wo_a.pk])).status_code == 404
    response = client.post(reverse("work_order_assignees", args=[wo_a.pk]), {"helpers": []})
    assert response.status_code == 404


def test_b_cannot_pick_a_location_of_a_in_the_forms(client, tenant_a, tenant_b, area_a):
    login(client, tenant_b.admin)
    assert str(area_a.pk) not in client.get(reverse("work_order_create")).content.decode()
    assert str(area_a.pk) not in client.get(reverse("location_report")).content.decode()
    response = client.post(
        reverse("work_order_create"),
        {"work_type": "corrective", "location": str(area_a.pk), "title": "x", "priority": "3"},
    )
    assert response.status_code == 200 and "location" in response.context["form"].errors
    response = client.post(reverse("location_report"), {"location": str(area_a.pk), "description": "x"})
    assert response.status_code == 200 and "location" in response.context["form"].errors
    with tenant_context(tenant_b.org):
        assert not WorkOrder.objects.exists() and not WorkRequest.objects.exists()


def test_b_cannot_pick_people_of_a_in_the_forms(client, tenant_a, tenant_b):
    with tenant_context(tenant_b.org):
        wo = create_work_order(work_type="corrective", title="x", asset=tenant_b.asset)
    login(client, tenant_b.admin)
    response = client.post(
        reverse("work_order_assignees", args=[wo.pk]), {"helpers": [str(tenant_a.tech.user_id)]}
    )
    assert response.status_code == 400 and "helpers" in response.context["assignees_form"].errors
    response = client.post(
        reverse("work_order_create"),
        {
            "work_type": "corrective",
            "asset": str(tenant_b.asset.pk),
            "title": "x",
            "priority": "3",
            "helpers": [str(tenant_a.tech.user_id)],
        },
    )
    assert response.status_code == 200 and "helpers" in response.context["form"].errors


def test_location_requests_of_a_are_invisible_to_b(client, tenant_a, tenant_b, area_a):
    with tenant_context(tenant_a.org):
        request = report_failure(membership=tenant_a.viewer, location=area_a, description="x")
    login(client, tenant_b.admin)
    assert request.pk not in {r.pk for r in client.get(reverse("request_list")).context["page"].object_list}
    assert client.post(reverse("request_accept", args=[request.pk])).status_code == 404
