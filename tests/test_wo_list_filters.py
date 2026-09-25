"""Filtros y paginación de la lista de OT (WO-05)."""

import datetime
from dataclasses import dataclass

import pytest
from django.urls import reverse

from apps.accounts.models import Membership, User
from apps.assets.models import Asset
from apps.core.tenancy import tenant_context
from apps.organizations.models import Location
from apps.work.models import WorkOrder
from apps.work.services import create_work_order
from tests.conftest import login


@dataclass
class Scene:
    north: Location
    south: Location
    asset_north: Asset
    asset_south: Asset
    supervisor: Membership
    wo_open_north: WorkOrder
    wo_overdue_south: WorkOrder
    wo_done: WorkOrder


@pytest.fixture
def scene(tenant_a):
    """Planta Norte (con un área hija) y Planta Sur, cada una con un activo y una OT."""
    with tenant_context(tenant_a.org):
        north = Location.objects.create(code="N", name="Planta Norte")
        area = Location.objects.create(code="N-A", name="Envasado", kind="area", parent=north)
        south = Location.objects.create(code="S", name="Planta Sur")
        cls = tenant_a.asset.asset_class
        asset_north = Asset.objects.create(code="AN-01", name="Llenadora", asset_class=cls, location=area)
        asset_south = Asset.objects.create(code="AS-01", name="Caldera", asset_class=cls, location=south)

        supervisor_user = User.objects.create_user("supervisor@empresa-a.cl", full_name="Sonia Supervisor")
        supervisor = Membership.objects.create(
            user=supervisor_user, organization=tenant_a.org, role="supervisor"
        )

        wo_open_north = create_work_order(
            work_type="corrective",
            title="Fuga en norte",
            asset=asset_north,
            status="planned",
            due_on=datetime.date(2026, 10, 5),
            assigned_to=tenant_a.tech.user,
        )
        wo_overdue_south = create_work_order(
            work_type="preventive",
            title="Mantención sur",
            asset=asset_south,
            status="scheduled",
            due_on=datetime.date(2026, 1, 1),
            assigned_to=supervisor_user,
        )
        wo_done = create_work_order(
            work_type="corrective", title="Cerrada", asset=asset_north, status="closed"
        )
    return Scene(north, south, asset_north, asset_south, supervisor, wo_open_north, wo_overdue_south, wo_done)


def _pks(response):
    return {wo.pk for wo in response.context["page"].object_list}


def test_default_scope_for_technician_is_mine(client, tenant_a, scene):
    login(client, tenant_a.tech)
    assert _pks(client.get(reverse("work_order_list"))) == {scene.wo_open_north.pk}


def test_default_scope_for_other_roles_is_open(client, tenant_a, scene):
    login(client, tenant_a.admin)
    pks = _pks(client.get(reverse("work_order_list")))
    assert {scene.wo_open_north.pk, scene.wo_overdue_south.pk} <= pks
    assert scene.wo_done.pk not in pks


def test_mine_chip_still_works_for_any_role(client, tenant_a, scene):
    login(client, tenant_a.admin)
    response = client.get(reverse("work_order_list"), {"scope": "mine"})
    assert scene.wo_open_north.pk not in _pks(response)  # asignada al técnico, no al admin


def test_filter_by_status(client, tenant_a, scene):
    login(client, tenant_a.admin)
    response = client.get(reverse("work_order_list"), {"scope": "open", "status": "scheduled"})
    assert _pks(response) == {scene.wo_overdue_south.pk}


def test_filter_by_status_outside_every_scope_bucket(client, tenant_a, scene):
    """ "cancelled" no está en ningún balde de scope: el filtro de estado debe poder encontrarla igual."""
    with tenant_context(tenant_a.org):
        cancelled = create_work_order(
            work_type="corrective", title="Cancelada", asset=scene.asset_north, status="cancelled"
        )
    login(client, tenant_a.admin)
    response = client.get(reverse("work_order_list"), {"scope": "open", "status": "cancelled"})
    assert _pks(response) == {cancelled.pk}


def test_filter_by_type(client, tenant_a, scene):
    login(client, tenant_a.admin)
    response = client.get(reverse("work_order_list"), {"scope": "open", "type": "preventive"})
    assert _pks(response) == {scene.wo_overdue_south.pk}


def test_filter_by_asset(client, tenant_a, scene):
    login(client, tenant_a.admin)
    response = client.get(reverse("work_order_list"), {"scope": "open", "asset_q": "caldera"})
    assert _pks(response) == {scene.wo_overdue_south.pk}


def test_filter_by_site_includes_descendants(client, tenant_a, scene):
    login(client, tenant_a.admin)
    response = client.get(reverse("work_order_list"), {"scope": "open", "location": scene.north.pk})
    assert _pks(response) == {scene.wo_open_north.pk}  # el activo está en el área hija de Planta Norte


def test_filter_by_responsible(client, tenant_a, scene):
    login(client, tenant_a.admin)
    response = client.get(
        reverse("work_order_list"), {"scope": "open", "assigned_to": scene.supervisor.user_id}
    )
    assert _pks(response) == {scene.wo_overdue_south.pk}


def test_filter_by_due_date_range(client, tenant_a, scene):
    login(client, tenant_a.admin)
    response = client.get(
        reverse("work_order_list"),
        {"scope": "open", "date_field": "due_on", "date_from": "2026-10-01", "date_to": "2026-10-31"},
    )
    assert _pks(response) == {scene.wo_open_north.pk}


def test_filter_by_created_date_range(client, tenant_a, scene):
    with tenant_context(tenant_a.org):
        WorkOrder.objects.filter(pk=scene.wo_overdue_south.pk).update(
            created_at=datetime.datetime(2025, 1, 1, 12, tzinfo=datetime.UTC)
        )
    login(client, tenant_a.admin)
    response = client.get(
        reverse("work_order_list"),
        {"scope": "open", "date_field": "created_at", "date_from": "2025-01-01", "date_to": "2025-01-02"},
    )
    assert _pks(response) == {scene.wo_overdue_south.pk}


def test_invalid_date_range_shows_error_and_keeps_other_filters(client, tenant_a, scene):
    login(client, tenant_a.admin)
    response = client.get(
        reverse("work_order_list"),
        {"scope": "open", "date_from": "2026-12-31", "date_to": "2026-01-01"},
    )
    assert response.status_code == 200
    assert "no puede ser posterior" in response.content.decode()


def test_pagination_is_always_applied(client, tenant_a):
    with tenant_context(tenant_a.org):
        for i in range(30):
            create_work_order(work_type="corrective", title=f"OT {i}", asset=tenant_a.asset)
    login(client, tenant_a.admin)
    page = client.get(reverse("work_order_list"), {"scope": "open"}).context["page"]
    assert page.paginator.per_page == 25
    assert page.paginator.count >= 30
    assert len(page.object_list) == 25
