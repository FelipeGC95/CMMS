"""Alta manual de OT (WO-01)."""

import pytest
from django.urls import reverse

from apps.core.tenancy import tenant_context
from apps.work.models import WorkOrder
from apps.work.services import create_work_order
from tests.conftest import login


def test_service_accepts_the_wo01_fields_and_sets_the_correlative(in_a):
    """CA1-CA3: tipo, activo, título, descripción, prioridad, fecha límite, estimación, responsable."""
    wo = create_work_order(
        work_type="improvement",
        title="Cambiar filtro",
        asset=in_a.asset,
        description="Filtro con más de 500 h de uso",
        priority=WorkOrder.Priority.HIGH,
        due_on=None,
        estimated_minutes=90,
        assigned_to=in_a.tech.user,
    )
    assert wo.work_type == "improvement"
    assert wo.code.startswith("OT-M-")  # CA3: correlativo por organización y tipo
    assert wo.status == WorkOrder.Status.PLANNED
    assert wo.priority == WorkOrder.Priority.HIGH
    assert wo.estimated_minutes == 90
    assert wo.assigned_to == in_a.tech.user


def test_create_view_get_renders_the_form(client, tenant_a):
    login(client, tenant_a.admin)
    response = client.get(reverse("work_order_create"))
    assert response.status_code == 200
    assert "form" in response.context


def test_create_view_creates_a_work_order_and_redirects_to_its_detail(client, tenant_a):
    login(client, tenant_a.admin)
    response = client.post(
        reverse("work_order_create"),
        {
            "work_type": "improvement",
            "asset": tenant_a.asset.pk,
            "title": "Cambiar filtro",
            "description": "Filtro con más de 500 h de uso",
            "priority": "2",
            "due_on": "2026-11-01",
            "estimated_minutes": "90",
        },
    )
    with tenant_context(tenant_a.org):
        wo = WorkOrder.objects.get(title="Cambiar filtro")
    assert response.status_code == 302
    assert response["Location"] == reverse("work_order_detail", args=[wo.pk])
    assert wo.work_type == "improvement"
    assert wo.status == WorkOrder.Status.PLANNED
    assert wo.priority == 2
    assert wo.asset_id == tenant_a.asset.pk
    assert wo.estimated_minutes == 90


def test_create_view_assigns_a_responsible(client, tenant_a):
    login(client, tenant_a.admin)
    response = client.post(
        reverse("work_order_create"),
        {
            "work_type": "corrective",
            "asset": tenant_a.asset.pk,
            "title": "Fuga",
            "priority": "1",
            "assigned_to": tenant_a.tech.user_id,
        },
    )
    assert response.status_code == 302
    with tenant_context(tenant_a.org):
        wo = WorkOrder.objects.get(title="Fuga")
    assert wo.assigned_to_id == tenant_a.tech.user_id


def test_missing_required_fields_shows_errors_without_creating(client, tenant_a):
    login(client, tenant_a.admin)
    with tenant_context(tenant_a.org):
        before = WorkOrder.objects.count()
    response = client.post(reverse("work_order_create"), {"work_type": "corrective"})
    assert response.status_code == 200
    assert response.context["form"].errors
    with tenant_context(tenant_a.org):
        assert WorkOrder.objects.count() == before


@pytest.mark.parametrize("role", ["technician", "viewer", "external"])
def test_roles_without_work_order_manage_cannot_create(client, tenant_a, role):
    member = tenant_a.tech
    member.role = role
    member.save()
    login(client, member)
    assert client.get(reverse("work_order_create")).status_code == 403
    assert client.post(reverse("work_order_create"), {}).status_code == 403


@pytest.mark.parametrize("role", ["admin", "planner", "supervisor"])
def test_roles_with_work_order_manage_can_open_the_form(client, tenant_a, role):
    member = tenant_a.tech
    member.role = role
    member.save()
    login(client, member)
    assert client.get(reverse("work_order_create")).status_code == 200


def test_cannot_create_a_work_order_on_another_organizations_asset(client, tenant_a, tenant_b):
    login(client, tenant_a.admin)
    with tenant_context(tenant_a.org):
        before = WorkOrder.objects.count()
    response = client.post(
        reverse("work_order_create"),
        {"work_type": "corrective", "asset": str(tenant_b.asset.pk), "title": "x", "priority": "3"},
    )
    assert response.status_code == 200
    assert "asset" in response.context["form"].errors
    with tenant_context(tenant_a.org):
        assert WorkOrder.objects.count() == before
