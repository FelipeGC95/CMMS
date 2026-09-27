"""Notificaciones en la app y por correo (X-02)."""

import datetime

import pytest
from django.core import mail
from django.core.management import call_command
from django.urls import reverse
from procrastinate.contrib.django import app

from apps.accounts.models import Membership, MembershipLocation, User
from apps.core.tenancy import tenant_context
from apps.notifications import tasks
from apps.notifications.models import Kind, Notification, NotificationPreference
from apps.notifications.selectors import unread_count
from apps.notifications.services import save_preferences, send_due_notifications
from apps.organizations.models import Location
from apps.work.models import WorkOrder
from apps.work.services import (
    complete_work_order,
    create_work_order,
    reject_request,
    report_failure,
    set_assignees,
)
from tests.conftest import PASSWORD, login


@pytest.fixture
def run_on_commit(django_capture_on_commit_callbacks):
    def run(func, **kwargs):
        with django_capture_on_commit_callbacks(execute=True):
            return func(**kwargs)

    return run


def member(tenant, role, email, *, locations=()):
    user = User.objects.create_user(email, PASSWORD, full_name=email.split("@")[0])
    membership = Membership.objects.create(
        user=user, organization=tenant.org, role=role, restricted_to_locations=bool(locations)
    )
    for location in locations:
        MembershipLocation.objects.create(membership=membership, location=location)
    return membership


def notes(user, kind=None):
    qs = Notification.objects.filter(recipient=user)
    return qs.filter(kind=kind) if kind else qs


# --- OT asignada ----------------------------------------------------------------


def test_assignment_on_create_notifies_lead_and_helpers_but_not_the_author(in_a, run_on_commit):
    helper = member(in_a, "technician", "apoyo@empresa-a.cl")
    wo = run_on_commit(
        create_work_order,
        work_type="corrective",
        title="Rodamiento",
        asset=in_a.asset,
        assigned_to=in_a.tech.user,
        helpers=[helper.user, in_a.admin.user],
        by=in_a.admin.user,
    )
    assert notes(in_a.tech.user, Kind.WO_ASSIGNED).get().work_order == wo
    assert notes(helper.user, Kind.WO_ASSIGNED).count() == 1
    assert not notes(in_a.admin.user).exists()
    note = notes(in_a.tech.user).get()
    assert wo.code in note.title and note.url == reverse("work_order_detail", args=[wo.pk])
    assert {m.to[0] for m in mail.outbox} == {in_a.tech.user.email, helper.user.email}
    assert wo.code in mail.outbox[0].subject
    assert mail.outbox[0].alternatives  # versión HTML


def test_reassignment_only_notifies_new_people(in_a, run_on_commit):
    wo = create_work_order(work_type="corrective", title="R", asset=in_a.asset, assigned_to=in_a.tech.user)
    helper = member(in_a, "technician", "apoyo@empresa-a.cl")
    run_on_commit(
        set_assignees, work_order=wo, membership=in_a.admin, assigned_to=in_a.tech.user, helpers=[helper.user]
    )
    assert not notes(in_a.tech.user).exists()
    assert notes(helper.user, Kind.WO_ASSIGNED).count() == 1


def test_nothing_is_notified_if_the_transaction_rolls_back(in_a, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=False) as callbacks:
        create_work_order(work_type="corrective", title="R", asset=in_a.asset, assigned_to=in_a.tech.user)
    assert callbacks  # se entrega recién al confirmar
    assert not Notification.objects.exists()


def test_out_of_scope_assignee_is_not_notified(in_a, run_on_commit):
    site = Location.objects.create(code="S1", name="Planta 1")
    other = Location.objects.create(code="S2", name="Planta 2")
    in_a.asset.location = site
    in_a.asset.save()
    scoped = member(in_a, "technician", "sitio2@empresa-a.cl", locations=[other])
    run_on_commit(
        create_work_order, work_type="corrective", title="R", asset=in_a.asset, assigned_to=scoped.user
    )
    assert not notes(scoped.user).exists()


# --- Solicitudes ------------------------------------------------------------------


def test_new_request_notifies_managers_in_scope_only(in_a, run_on_commit):
    site = Location.objects.create(code="S1", name="Planta 1")
    other = Location.objects.create(code="S2", name="Planta 2")
    planner = member(in_a, "planner", "planner@empresa-a.cl")
    supervisor_here = member(in_a, "supervisor", "sup1@empresa-a.cl", locations=[site])
    supervisor_there = member(in_a, "supervisor", "sup2@empresa-a.cl", locations=[other])
    request = run_on_commit(report_failure, membership=in_a.viewer, description="Gotera", location=site)
    for membership in (in_a.admin, planner, supervisor_here):
        assert notes(membership.user, Kind.REQUEST_NEW).get().work_request == request
    for membership in (supervisor_there, in_a.tech, in_a.viewer):
        assert not notes(membership.user).exists()


def test_rejected_request_notifies_the_requester(in_a, run_on_commit):
    request = report_failure(membership=in_a.viewer, description="Ruido", asset=in_a.asset)
    run_on_commit(reject_request, work_request=request, membership=in_a.admin, reason="Duplicada")
    note = notes(in_a.viewer.user, Kind.REQUEST_RESOLVED).get()
    assert f"#{request.number}" in note.title and note.body == "Duplicada"


def test_completed_request_work_order_notifies_the_requester(in_a, run_on_commit):
    request = report_failure(membership=in_a.viewer, description="Ruido", asset=in_a.asset)
    run_on_commit(complete_work_order, work_order=request.work_order, membership=in_a.tech, labor_minutes=0)
    assert notes(in_a.viewer.user, Kind.REQUEST_RESOLVED).count() == 1


# --- Preferencias -------------------------------------------------------------------


def test_preferences_choose_the_channels(in_a, run_on_commit):
    save_preferences(membership=in_a.tech, values={Kind.WO_ASSIGNED: (True, False)})
    run_on_commit(
        create_work_order, work_type="corrective", title="R", asset=in_a.asset, assigned_to=in_a.tech.user
    )
    assert notes(in_a.tech.user).get().in_app
    assert mail.outbox == []

    save_preferences(membership=in_a.tech, values={Kind.WO_ASSIGNED: (False, True)})
    run_on_commit(
        create_work_order, work_type="corrective", title="R", asset=in_a.asset, assigned_to=in_a.tech.user
    )
    assert notes(in_a.tech.user).filter(in_app=False).count() == 1  # solo correo: no aparece en la bandeja
    assert len(mail.outbox) == 1 and unread_count(membership=in_a.tech) == 1

    save_preferences(membership=in_a.tech, values={Kind.WO_ASSIGNED: (False, False)})
    run_on_commit(
        create_work_order, work_type="corrective", title="R", asset=in_a.asset, assigned_to=in_a.tech.user
    )
    assert notes(in_a.tech.user).count() == 2 and len(mail.outbox) == 1


# --- Vencimientos ---------------------------------------------------------------------


def _due(tenant, days, **kwargs):
    today = datetime.date(2026, 9, 24)
    return create_work_order(
        work_type="corrective",
        title=f"Vence {days}",
        asset=tenant.asset,
        due_on=today + datetime.timedelta(days=days),
        **kwargs,
    )


def test_due_notifications_are_daily_and_idempotent(in_a):
    today = datetime.date(2026, 9, 24)
    overdue = _due(in_a, -2, assigned_to=in_a.tech.user)
    soon = _due(in_a, 1, assigned_to=in_a.tech.user)
    _due(in_a, 5, assigned_to=in_a.tech.user)  # lejos: no avisa
    closed = _due(in_a, -1, assigned_to=in_a.tech.user)
    WorkOrder.objects.filter(pk=closed.pk).update(status=WorkOrder.Status.CANCELLED)

    assert send_due_notifications(today=today) == 2
    assert notes(in_a.tech.user, Kind.WO_OVERDUE).get().work_order == overdue
    assert notes(in_a.tech.user, Kind.WO_DUE_SOON).get().work_order == soon
    assert len(mail.outbox) == 1  # un resumen por persona
    assert overdue.code in mail.outbox[0].body and soon.code in mail.outbox[0].body

    assert send_due_notifications(today=today) == 0  # mismo día: nada nuevo
    assert len(mail.outbox) == 1
    assert send_due_notifications(today=today + datetime.timedelta(days=1)) == 2  # al día siguiente, de nuevo


def test_unassigned_due_work_order_goes_to_managers(in_a):
    _due(in_a, 0)
    send_due_notifications(today=datetime.date(2026, 9, 24))
    assert notes(in_a.admin.user, Kind.WO_DUE_SOON).count() == 1
    assert not notes(in_a.tech.user).exists()


def test_command_and_periodic_task(tenant_a, tenant_b, monkeypatch):
    with tenant_context(tenant_a.org):
        _due(tenant_a, -30, assigned_to=tenant_a.tech.user)
    call_command("send_due_notifications")
    with tenant_context(tenant_a.org):
        assert notes(tenant_a.tech.user, Kind.WO_OVERDUE).count() == 1
    assert tasks.send_due_notifications_for(organization_id=str(tenant_a.org.pk)) == 0  # idempotente

    periodic = {p.task.name: p for p in app.periodic_registry.periodic_tasks.values()}
    assert periodic["notifications.enqueue_due_notifications"].cron == "0 11 * * *"
    deferred = []

    class Deferrer:
        def __init__(self, lock):
            self.lock = lock

        def defer(self, **kwargs):
            deferred.append((self.lock, kwargs))

    monkeypatch.setattr(
        tasks.send_due_notifications_for, "configure", lambda queueing_lock: Deferrer(queueing_lock)
    )
    assert tasks.enqueue_due_notifications(timestamp=0) == 2
    assert {kwargs["organization_id"] for _lock, kwargs in deferred} == {
        str(tenant_a.org.pk),
        str(tenant_b.org.pk),
    }


# --- Vistas ---------------------------------------------------------------------------


def _note(tenant, membership, title="Aviso", url="/work-orders/"):
    with tenant_context(tenant.org):
        return Notification.objects.create(
            recipient=membership.user, kind=Kind.WO_ASSIGNED, title=title, url=url
        )


def test_list_badge_open_and_read_all(client, tenant_a):
    first = _note(tenant_a, tenant_a.tech, "Primera")
    _note(tenant_a, tenant_a.tech, "Segunda")
    login(client, tenant_a.tech)
    response = client.get(reverse("notification_list"))
    assert response.status_code == 200 and b"Primera" in response.content
    assert b">2<" in client.get(reverse("notification_badge")).content

    response = client.post(reverse("notification_open", args=[first.pk]))
    assert response.status_code == 302 and response.url == "/work-orders/"
    assert b">1<" in client.get(reverse("notification_badge")).content

    client.post(reverse("notification_read_all"))
    assert b"bell-count" not in client.get(reverse("notification_badge")).content


def test_open_never_redirects_off_site(client, tenant_a):
    note = _note(tenant_a, tenant_a.tech, url="https://malicioso.example/")
    login(client, tenant_a.tech)
    assert client.post(reverse("notification_open", args=[note.pk])).url == reverse("notification_list")


def test_preferences_page_saves_own_preferences(client, tenant_a):
    login(client, tenant_a.tech)
    assert client.get(reverse("notification_preferences")).status_code == 200
    client.post(reverse("notification_preferences"), {f"{Kind.WO_ASSIGNED}-in_app": "on"})
    with tenant_context(tenant_a.org):
        pref = NotificationPreference.objects.get(user=tenant_a.tech.user, kind=Kind.WO_ASSIGNED)
        assert (pref.in_app, pref.email) == (True, False)
        assert NotificationPreference.objects.filter(user=tenant_a.tech.user).count() == len(Kind.values)
        assert not NotificationPreference.objects.exclude(user=tenant_a.tech.user).exists()


def test_bell_is_in_the_header_and_menu(client, tenant_a):
    login(client, tenant_a.tech)
    page = client.get(reverse("dashboard")).content.decode()
    assert reverse("notification_badge") in page and 'class="navbar__bell"' in page
    menu = client.get(reverse("menu")).content.decode()
    assert reverse("notification_list") in menu and reverse("notification_preferences") in menu
