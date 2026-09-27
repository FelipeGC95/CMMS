"""Aislamiento de notificaciones (X-02): lo de A nunca llega a B ni se ve desde B."""

import datetime

import pytest
from django.urls import reverse

from apps.accounts.models import Membership
from apps.core.tenancy import platform_context, tenant_context
from apps.notifications.models import Kind, Notification
from apps.notifications.services import send_due_notifications
from apps.work.services import WorkOrderError, create_work_order, report_failure
from tests.conftest import login


def test_events_in_a_never_notify_users_of_b(tenant_a, tenant_b, django_capture_on_commit_callbacks):
    # Un administrador de B que además es técnico en A solo recibe avisos como miembro de A.
    Membership.objects.create(user=tenant_b.admin.user, organization=tenant_a.org, role="technician")
    with tenant_context(tenant_a.org), django_capture_on_commit_callbacks(execute=True):
        report_failure(membership=tenant_a.viewer, description="Ruido", asset=tenant_a.asset)
        create_work_order(
            work_type="corrective",
            title="R",
            asset=tenant_a.asset,
            assigned_to=tenant_a.tech.user,
            due_on=datetime.date(2026, 9, 20),
        )
        send_due_notifications(today=datetime.date(2026, 9, 24))
    assert Notification.all_tenants.exists()
    assert not Notification.all_tenants.filter(organization=tenant_b.org).exists()
    b_only = [tenant_b.tech.user, tenant_b.viewer.user]
    assert not Notification.all_tenants.filter(recipient__in=b_only).exists()


def test_assignee_from_another_organization_is_rejected_and_not_notified(tenant_a, tenant_b):
    with tenant_context(tenant_a.org):
        with pytest.raises(WorkOrderError):
            create_work_order(
                work_type="corrective", title="R", asset=tenant_a.asset, assigned_to=tenant_b.tech.user
            )
    assert not Notification.all_tenants.filter(recipient=tenant_b.tech.user).exists()


def test_list_badge_and_open_show_only_own_notifications_of_the_active_org(client, tenant_a, tenant_b):
    with tenant_context(tenant_a.org):
        other_user = Notification.objects.create(
            recipient=tenant_a.admin.user, kind=Kind.WO_ASSIGNED, title="De otra persona"
        )
        own = Notification.objects.create(
            recipient=tenant_a.tech.user, kind=Kind.WO_ASSIGNED, title="Propia en A"
        )
    with tenant_context(tenant_b.org):
        foreign = Notification.objects.create(
            recipient=tenant_b.tech.user, kind=Kind.WO_ASSIGNED, title="De B"
        )
    login(client, tenant_a.tech)
    page = client.get(reverse("notification_list")).content
    assert b"Propia en A" in page and b"De otra persona" not in page and b"De B" not in page
    assert b">1<" in client.get(reverse("notification_badge")).content
    for note in (other_user, foreign):
        assert client.post(reverse("notification_open", args=[note.pk])).status_code == 404
    client.post(reverse("notification_read_all"))
    with tenant_context(tenant_a.org):
        own.refresh_from_db()
        other_user.refresh_from_db()
    assert own.read_at and other_user.read_at is None
    # Fuera del contexto de la petición la fila solo se ve con bypass de RLS (ADR-026).
    with platform_context():
        foreign.refresh_from_db()
    assert foreign.read_at is None


def test_org_link_of_b_never_shows_b_data_to_a_non_member(client, tenant_a, tenant_b):
    """Un enlace con la organización B (`?org=`) no da acceso a B a quien no es miembro."""
    with tenant_context(tenant_b.org):
        wo_b = create_work_order(work_type="corrective", title="Secreto de B", asset=tenant_b.asset)
    login(client, tenant_a.admin)
    detail = reverse("work_order_detail", args=[wo_b.pk])
    response = client.get(detail, {"org": str(tenant_b.org.pk)}, follow=True)
    assert response.status_code == 404
    assert "Secreto de B" not in response.content.decode()
    assert client.session["active_organization_id"] == str(tenant_a.org.pk)
    for garbage in ("", "no-es-uuid", tenant_b.org.slug):
        response = client.get(reverse("work_order_list"), {"org": garbage}, follow=True)
        assert response.status_code == 200 and "Secreto de B" not in response.content.decode()
        assert client.session["active_organization_id"] == str(tenant_a.org.pk)
