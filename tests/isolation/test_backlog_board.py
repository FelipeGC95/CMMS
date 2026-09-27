"""Aislamiento del backlog y la programación semanal (REQ-04/05): B no ve ni programa lo de A."""

import pytest
from django.urls import reverse

from apps.core.tenancy import tenant_context
from apps.notifications.models import Kind, Notification
from apps.work.models import WorkOrder
from apps.work.selectors import backlog, technicians_in_scope
from apps.work.services import WorkOrderError, create_work_order, schedule_work_order
from tests.conftest import login
from tests.test_backlog_board import next_monday


@pytest.fixture
def wo_a(tenant_a):
    with tenant_context(tenant_a.org):
        return create_work_order(
            work_type="corrective",
            title="Secreto de A",
            asset=tenant_a.asset,
            assigned_to=tenant_a.tech.user,
            estimated_minutes=120,
        )


def test_b_backlog_and_capacity_exclude_a(tenant_a, tenant_b, wo_a):
    with tenant_context(tenant_b.org):
        assert list(backlog(membership=tenant_b.admin)) == []
        assert [m.pk for m in technicians_in_scope(membership=tenant_b.admin)] == [tenant_b.tech.pk]


@pytest.mark.parametrize(
    "url_name", ["work_order_backlog", "work_order_board", "work_order_backlog_export_xlsx"]
)
def test_b_screens_do_not_show_a(client, tenant_a, tenant_b, wo_a, url_name):
    with tenant_context(tenant_a.org):
        schedule_work_order(work_order=wo_a, membership=tenant_a.admin, scheduled_for=next_monday())
    login(client, tenant_b.admin)
    response = client.get(reverse(url_name) + f"?week={next_monday().isoformat()}")
    assert response.status_code == 200
    if url_name != "work_order_backlog_export_xlsx":
        body = response.content.decode()
        assert "Secreto de A" not in body
        assert tenant_a.tech.user.full_name not in body


def test_b_cannot_schedule_or_unschedule_a_work_order(client, tenant_a, tenant_b, wo_a):
    login(client, tenant_b.admin)
    day = next_monday().isoformat()
    assert (
        client.post(reverse("work_order_schedule", args=[wo_a.pk]), {"scheduled_for": day}).status_code == 404
    )
    assert client.post(reverse("work_order_unschedule", args=[wo_a.pk])).status_code == 404
    with tenant_context(tenant_a.org):
        wo_a.refresh_from_db()
    assert (wo_a.status, wo_a.scheduled_for) == (WorkOrder.Status.PLANNED, None)


def test_b_cannot_assign_its_technician_to_a_work_order(tenant_a, tenant_b, wo_a):
    with tenant_context(tenant_a.org):
        with pytest.raises(WorkOrderError):
            schedule_work_order(
                work_order=wo_a,
                membership=tenant_a.admin,
                scheduled_for=next_monday(),
                assigned_to=tenant_b.tech.user,
            )
        wo_a.refresh_from_db()
    assert wo_a.assigned_to == tenant_a.tech.user


def test_b_form_cannot_pick_a_technician(client, tenant_a, tenant_b):
    with tenant_context(tenant_b.org):
        wo_b = create_work_order(work_type="corrective", title="B", asset=tenant_b.asset)
    login(client, tenant_b.admin)
    response = client.post(
        reverse("work_order_schedule", args=[wo_b.pk]),
        {"scheduled_for": next_monday().isoformat(), "assigned_to": tenant_a.tech.user.pk},
    )
    assert response.status_code == 302
    with tenant_context(tenant_b.org):
        wo_b.refresh_from_db()
    assert (wo_b.status, wo_b.assigned_to) == (WorkOrder.Status.PLANNED, None)


def test_scheduling_in_a_notifies_nobody_in_b(tenant_a, tenant_b, wo_a, django_capture_on_commit_callbacks):
    with tenant_context(tenant_a.org), django_capture_on_commit_callbacks(execute=True):
        schedule_work_order(work_order=wo_a, membership=tenant_a.admin, scheduled_for=next_monday())
    assert Notification.all_tenants.filter(kind=Kind.WO_SCHEDULED, organization=tenant_a.org).count() == 1
    assert not Notification.all_tenants.filter(organization=tenant_b.org).exists()


def test_b_my_week_does_not_show_a(client, tenant_a, tenant_b, wo_a):
    with tenant_context(tenant_a.org):
        schedule_work_order(work_order=wo_a, membership=tenant_a.admin, scheduled_for=next_monday())
    login(client, tenant_b.tech)
    body = client.get(reverse("work_order_my_week") + f"?week={next_monday().isoformat()}").content.decode()
    assert "Secreto de A" not in body


def test_b_lanes_do_not_show_a_and_drop_cannot_assign_a_technician(client, tenant_a, tenant_b, wo_a):
    """Vista «Por técnico»: B no ve filas de A ni puede soltar una OT sobre un técnico de A."""
    with tenant_context(tenant_a.org):
        schedule_work_order(work_order=wo_a, membership=tenant_a.admin, scheduled_for=next_monday())
    with tenant_context(tenant_b.org):
        wo_b = create_work_order(work_type="corrective", title="B", asset=tenant_b.asset)
    login(client, tenant_b.admin)
    url = reverse("work_order_board") + f"?week={next_monday().isoformat()}&lanes=1"
    body = client.get(url).content.decode()
    assert "Secreto de A" not in body and tenant_a.tech.user.full_name not in body
    assert f'data-drop-assignee="{tenant_a.tech.user.pk}"' not in body
    assert f'data-drop-assignee="{tenant_b.tech.user.pk}"' in body
    response = client.post(
        reverse("work_order_schedule", args=[wo_b.pk]),
        {"scheduled_for": next_monday().isoformat(), "assigned_to": tenant_a.tech.user.pk},
        HTTP_X_BOARD_DROP="1",
    )
    assert response.status_code == 400
    with tenant_context(tenant_b.org):
        wo_b.refresh_from_db()
    assert (wo_b.status, wo_b.assigned_to) == (WorkOrder.Status.PLANNED, None)
