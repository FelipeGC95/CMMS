"""Backlog priorizado (REQ-04) y programación semanal (REQ-05)."""

import datetime
from decimal import Decimal

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Membership, MembershipLocation, User
from apps.accounts.services import update_member
from apps.assets.models import Asset
from apps.audit.models import AuditEvent
from apps.core.tenancy import tenant_context
from apps.notifications.models import Kind, Notification, NotificationPreference
from apps.organizations.models import Location
from apps.work.models import WorkOrder, WorkOrderTransition
from apps.work.selectors import (
    backlog,
    backlog_summary,
    daily_capacity_minutes,
    technician_load,
    technicians_in_scope,
    unscheduled_backlog,
    week_start,
)
from apps.work.services import (
    WorkOrderError,
    create_work_order,
    schedule_work_order,
    transition,
    unschedule_work_order,
)
from tests.conftest import PASSWORD, login

S = WorkOrder.Status


def next_monday():
    return week_start(timezone.localdate()) + datetime.timedelta(days=7)


def _asset(tenant, code, criticality, location=None):
    return Asset.objects.create(
        code=code, name=code, asset_class=tenant.asset.asset_class, criticality=criticality, location=location
    )


def _wo(title, **kwargs):
    kwargs.setdefault("work_type", "corrective")
    return create_work_order(title=title, **kwargs)


# --- Puntaje y orden (REQ-04 CA1) --------------------------------------------------


def test_backlog_orders_by_criticality_times_priority_then_due_date(in_a):
    t = in_a
    b, c = _asset(t, "B-1", "B"), _asset(t, "C-1", "C")
    site = Location.objects.create(code="P1", name="Planta")
    day = datetime.date(2026, 10, 1)
    wo1 = _wo("A urgente", asset=t.asset, priority=1)  # 3 × 4 = 12
    wo2 = _wo("C urgente", asset=c, priority=1)  # 1 × 4 = 4
    wo3 = _wo("B alta", asset=b, priority=2, due_on=day + datetime.timedelta(days=5))  # 2 × 3 = 6
    wo4 = _wo("Ubicación alta", location=site, priority=2, due_on=day)  # B por defecto: 6
    wo5 = _wo("A baja sin fecha", asset=t.asset, priority=4)  # 3 × 1 = 3
    wo6 = _wo("A baja con fecha", asset=t.asset, priority=4, due_on=day)  # 3, gana por fecha
    for status in (S.CLOSED, S.CANCELLED, S.COMPLETED):
        WorkOrder.objects.filter(pk=_wo(status, asset=t.asset, priority=1).pk).update(status=status)

    rows = list(backlog(membership=t.admin))
    assert rows == [wo1, wo4, wo3, wo2, wo6, wo5]
    assert [r.score for r in rows] == [12, 6, 6, 4, 3, 3]


def test_backlog_filters_by_site_with_descendants_type_status_and_assignee(in_a):
    t = in_a
    site = Location.objects.create(code="P1", name="Planta")
    area = Location.objects.create(code="P1-A", name="Área", kind="area", parent=site)
    other = Location.objects.create(code="P2", name="Otra")
    in_area = _wo("En el área", asset=_asset(t, "X-1", "B", location=area), assigned_to=t.tech.user)
    on_site = _wo("Sobre el sitio", location=site, work_type="inspection")
    _wo("Otra planta", location=other)

    assert set(backlog(membership=t.admin, location=site)) == {in_area, on_site}
    assert list(backlog(membership=t.admin, work_type="inspection")) == [on_site]
    assert list(backlog(membership=t.admin, assigned_to=t.tech.user)) == [in_area]
    assert list(backlog(membership=t.admin, status=S.REQUESTED)) == []


# --- Horas y backlog en semanas (REQ-04 CA2) ---------------------------------------


def test_backlog_weeks_is_estimated_hours_over_team_weekly_capacity(in_a):
    t = in_a
    second = Membership.objects.create(
        user=User.objects.create_user("t2@a.cl", PASSWORD),
        organization=t.org,
        role="technician",
        weekly_capacity_hours=Decimal("20"),
    )
    _wo("1", asset=t.asset, estimated_minutes=600)  # 10 h
    _wo("2", asset=t.asset, estimated_minutes=1200)  # 20 h
    _wo("3", asset=t.asset)  # sin estimación: no suma horas
    technicians = technicians_in_scope(membership=t.admin)
    assert {m.pk for m in technicians} == {t.tech.pk, second.pk}  # admin y lectura no cuentan

    summary = backlog_summary(backlog(membership=t.admin), technicians=technicians)
    assert summary.count == 3
    assert summary.unestimated == 1
    assert summary.estimated_hours == Decimal("30.0")
    assert summary.weekly_capacity_hours == Decimal("60")  # 40 (por defecto) + 20
    assert summary.weeks == Decimal("0.5")


def test_backlog_weeks_is_empty_without_capacity(in_a):
    _wo("1", asset=in_a.asset, estimated_minutes=60)
    summary = backlog_summary(backlog(membership=in_a.admin), technicians=[])
    assert summary.weeks is None


def test_capacity_counts_only_technicians_covering_the_site(in_a):
    t = in_a
    site = Location.objects.create(code="P1", name="Planta")
    area = Location.objects.create(code="P1-A", name="Área", kind="area", parent=site)
    other = Location.objects.create(code="P2", name="Otra")
    scoped = []
    for email, location in (("en-planta@a.cl", site), ("en-otra@a.cl", other)):
        member = Membership.objects.create(
            user=User.objects.create_user(email, PASSWORD),
            organization=t.org,
            role="technician",
            restricted_to_locations=True,
        )
        MembershipLocation.objects.create(membership=member, location=location)
        scoped.append(member)
    # Filtrar por el área: cuenta quien cubre la planta completa (y el técnico sin alcance).
    ids = {m.pk for m in technicians_in_scope(membership=t.admin, location=area)}
    assert ids == {t.tech.pk, scoped[0].pk}


def test_admin_edits_weekly_capacity_in_team_screen(client, tenant_a):
    login(client, tenant_a.admin)
    url = reverse("member_edit", args=[tenant_a.tech.pk])
    response = client.post(url, {"role": "technician", "weekly_capacity_hours": "32.5"})
    assert response.status_code == 302
    tenant_a.tech.refresh_from_db()
    assert tenant_a.tech.weekly_capacity_hours == Decimal("32.5")
    with tenant_context(tenant_a.org):
        with pytest.raises(Exception, match="168"):
            update_member(
                membership=tenant_a.admin,
                target=tenant_a.tech,
                role="technician",
                restricted_to_locations=False,
                locations=[],
                weekly_capacity_hours=Decimal("200"),
            )


# --- Programar (REQ-05 CA2) --------------------------------------------------------


def test_scheduling_a_planned_work_order_transitions_and_notifies_assignee(
    in_a, django_capture_on_commit_callbacks
):
    t = in_a
    day = next_monday()
    wo = _wo("Cambiar correa", asset=t.asset, assigned_to=t.tech.user)
    with django_capture_on_commit_callbacks(execute=True):
        schedule_work_order(work_order=wo, membership=t.admin, scheduled_for=day)
    wo.refresh_from_db()
    assert wo.status == S.SCHEDULED
    assert wo.scheduled_for == day
    assert WorkOrderTransition.objects.filter(
        work_order=wo, from_status=S.PLANNED, to_status=S.SCHEDULED
    ).exists()
    assert AuditEvent.objects.filter(
        action=AuditEvent.Action.UPDATE, changes__has_key="scheduled_for"
    ).exists()
    notice = Notification.objects.get(kind=Kind.WO_SCHEDULED)
    assert notice.recipient == t.tech.user
    assert wo.code in notice.title


def test_scheduling_respects_the_notification_preference(in_a, django_capture_on_commit_callbacks):
    t = in_a
    NotificationPreference.objects.create(user=t.tech.user, kind=Kind.WO_SCHEDULED, in_app=False, email=False)
    wo = _wo("x", asset=t.asset, assigned_to=t.tech.user)
    with django_capture_on_commit_callbacks(execute=True):
        schedule_work_order(work_order=wo, membership=t.admin, scheduled_for=next_monday())
    assert not Notification.objects.filter(kind=Kind.WO_SCHEDULED).exists()


def test_scheduling_from_requested_and_rescheduling(in_a, django_capture_on_commit_callbacks):
    t = in_a
    day = next_monday()
    wo = _wo("x", asset=t.asset, status=S.REQUESTED)
    schedule_work_order(work_order=wo, membership=t.admin, scheduled_for=day, assigned_to=t.tech.user)
    wo.refresh_from_db()
    assert (wo.status, wo.scheduled_for, wo.assigned_to) == (S.SCHEDULED, day, t.tech.user)
    # Reprogramar: solo cambia el día, sin otra transición; se avisa otra vez.
    with django_capture_on_commit_callbacks(execute=True):
        schedule_work_order(work_order=wo, membership=t.admin, scheduled_for=day + datetime.timedelta(days=2))
    wo.refresh_from_db()
    assert wo.scheduled_for == day + datetime.timedelta(days=2)
    assert WorkOrderTransition.objects.filter(work_order=wo, to_status=S.SCHEDULED).count() == 1
    assert Notification.objects.filter(kind=Kind.WO_SCHEDULED, recipient=t.tech.user).count() == 1


@pytest.mark.parametrize("status", [S.IN_PROGRESS, S.ON_HOLD, S.COMPLETED, S.CLOSED, S.CANCELLED])
def test_started_or_finished_work_orders_cannot_be_scheduled(in_a, status):
    wo = _wo("x", asset=in_a.asset)
    WorkOrder.objects.filter(pk=wo.pk).update(status=status)
    wo.refresh_from_db()
    with pytest.raises(WorkOrderError, match="Solo se programan"):
        schedule_work_order(work_order=wo, membership=in_a.admin, scheduled_for=next_monday())


def test_past_days_are_rejected(in_a):
    wo = _wo("x", asset=in_a.asset)
    with pytest.raises(WorkOrderError, match="ya pasó"):
        schedule_work_order(
            work_order=wo,
            membership=in_a.admin,
            scheduled_for=timezone.localdate() - datetime.timedelta(days=1),
        )


def test_unschedule_returns_to_planned_without_day(in_a):
    t = in_a
    wo = _wo("x", asset=t.asset)
    schedule_work_order(work_order=wo, membership=t.admin, scheduled_for=next_monday())
    assert list(unscheduled_backlog(membership=t.admin)) == []
    unschedule_work_order(work_order=wo, membership=t.admin)
    wo.refresh_from_db()
    assert (wo.status, wo.scheduled_for) == (S.PLANNED, None)
    assert list(unscheduled_backlog(membership=t.admin)) == [wo]


def test_manual_transition_back_to_planned_clears_the_day(in_a):
    wo = _wo("x", asset=in_a.asset)
    schedule_work_order(work_order=wo, membership=in_a.admin, scheduled_for=next_monday())
    transition(work_order=wo, to_status=S.PLANNED, membership=in_a.admin)
    assert wo.scheduled_for is None


# --- Carga vs. capacidad (REQ-05 CA1) ----------------------------------------------


def test_load_per_technician_and_day_flags_over_capacity(in_a):
    t = in_a
    monday = next_monday()
    days = [monday + datetime.timedelta(days=i) for i in range(7)]
    for minutes in (300, 240):  # 9 h el lunes: sobre las 8 h diarias
        wo = _wo("x", asset=t.asset, assigned_to=t.tech.user, estimated_minutes=minutes)
        schedule_work_order(work_order=wo, membership=t.admin, scheduled_for=monday)
    wo = _wo("x", asset=t.asset, assigned_to=t.tech.user, estimated_minutes=60)
    schedule_work_order(work_order=wo, membership=t.admin, scheduled_for=days[5])  # sábado
    rows = technician_load(
        work_orders=WorkOrder.objects.filter(scheduled_for__isnull=False),
        technicians=[t.tech],
        days=days,
    )
    row = rows[0]
    assert (row.days[0].minutes, row.days[0].capacity_minutes, row.days[0].over) == (540, 480, True)
    assert (row.days[1].minutes, row.days[1].over) == (0, False)
    assert (row.days[5].capacity_minutes, row.days[5].over) == (0, True)  # fin de semana sin capacidad
    assert (row.week.minutes, row.week.capacity_minutes, row.week.over) == (600, 2400, False)
    assert daily_capacity_minutes(t.tech, days[6]) == 0


# --- Vistas ------------------------------------------------------------------------


def test_board_schedule_form_without_js(client, tenant_a):
    with tenant_context(tenant_a.org):
        wo = _wo("x", asset=tenant_a.asset)
    login(client, tenant_a.admin)
    day = next_monday()
    board = reverse("work_order_board") + f"?week={day.isoformat()}"
    response = client.post(
        reverse("work_order_schedule", args=[wo.pk]),
        {"scheduled_for": day.isoformat(), "assigned_to": tenant_a.tech.user.pk, "next": board},
    )
    assert response.status_code == 302 and response.url == board
    page = client.get(board)
    assert wo.code in page.content.decode()
    with tenant_context(tenant_a.org):
        wo.refresh_from_db()
    assert (wo.status, wo.assigned_to) == (S.SCHEDULED, tenant_a.tech.user)


def test_board_drop_returns_status_codes_for_fetch(client, tenant_a):
    with tenant_context(tenant_a.org):
        wo = _wo("x", asset=tenant_a.asset)
        started = _wo("y", asset=tenant_a.asset)
        WorkOrder.objects.filter(pk=started.pk).update(status=S.IN_PROGRESS)
    login(client, tenant_a.admin)
    headers = {"HTTP_X_BOARD_DROP": "1"}
    url = reverse("work_order_schedule", args=[wo.pk])
    assert client.post(url, {"scheduled_for": next_monday().isoformat()}, **headers).status_code == 204
    bad = client.post(
        reverse("work_order_schedule", args=[started.pk]),
        {"scheduled_for": next_monday().isoformat()},
        **headers,
    )
    assert bad.status_code == 400 and "Solo se programan" in bad.content.decode()
    assert client.post(reverse("work_order_unschedule", args=[wo.pk]), **headers).status_code == 204


def test_backlog_page_and_export(client, tenant_a):
    with tenant_context(tenant_a.org):
        _wo("Revisar bomba", asset=tenant_a.asset, estimated_minutes=90)
    login(client, tenant_a.admin)
    page = client.get(reverse("work_order_backlog"))
    assert page.status_code == 200
    assert "Revisar bomba" in page.content.decode()
    export = client.get(reverse("work_order_backlog_export_xlsx"))
    assert export.status_code == 200
    assert export["Content-Type"].startswith("application/vnd.openxmlformats")


def test_my_week_lists_own_scheduled_work_orders(client, tenant_a):
    with tenant_context(tenant_a.org):
        mine = _wo("Mía", asset=tenant_a.asset, assigned_to=tenant_a.tech.user)
        other = _wo("De otro", asset=tenant_a.asset, assigned_to=tenant_a.admin.user)
        for wo in (mine, other):
            schedule_work_order(work_order=wo, membership=tenant_a.admin, scheduled_for=next_monday())
    login(client, tenant_a.tech)
    body = client.get(reverse("work_order_my_week") + f"?week={next_monday().isoformat()}").content.decode()
    assert "Mía" in body and "De otro" not in body
    assert reverse("work_order_schedule", args=[mine.pk]) not in body  # solo lectura


# --- Tablero «Por técnico» (REQ-05 CA1) ---------------------------------------------


def test_board_lanes_group_by_technician_with_load_others_and_unassigned(in_a):
    from apps.work.selectors import board_lanes, scheduled_in_week

    t = in_a
    monday = next_monday()
    days = [monday + datetime.timedelta(days=i) for i in range(7)]
    mine = _wo("Del técnico", asset=t.asset, assigned_to=t.tech.user, estimated_minutes=540)
    boss = _wo("Del admin", asset=t.asset, assigned_to=t.admin.user, estimated_minutes=60)
    nobody = _wo("Sin nadie", asset=t.asset, estimated_minutes=30)
    for wo, day in ((mine, days[0]), (boss, days[1]), (nobody, days[2])):
        schedule_work_order(work_order=wo, membership=t.admin, scheduled_for=day)
    work_orders = list(scheduled_in_week(membership=t.admin, monday=monday))
    loads = technician_load(work_orders=work_orders, technicians=[t.tech], days=days)
    lanes = board_lanes(work_orders=work_orders, loads=loads, days=days, visible_days=days[:5])

    assert [lane.user for lane in lanes] == [t.tech.user, t.admin.user, None]
    tech, other, unassigned = lanes
    assert [c.day for c in tech.cells] == days[:5]
    assert tech.cells[0].work_orders == [mine]
    assert (tech.cells[0].load.minutes, tech.cells[0].load.over) == (540, True)
    assert (tech.week.minutes, tech.week.capacity_minutes) == (540, 2400)
    assert other.cells[1].work_orders == [boss] and other.week is None and other.cells[1].load is None
    assert unassigned.cells[2].work_orders == [nobody]


def test_board_with_lanes_renders_rows_and_drop_targets(client, tenant_a):
    with tenant_context(tenant_a.org):
        wo = _wo("Revisar bomba", asset=tenant_a.asset, assigned_to=tenant_a.tech.user, estimated_minutes=120)
        schedule_work_order(work_order=wo, membership=tenant_a.admin, scheduled_for=next_monday())
    login(client, tenant_a.admin)
    url = reverse("work_order_board") + f"?week={next_monday().isoformat()}&lanes=1"
    body = client.get(url).content.decode()
    assert "Revisar bomba" in body
    assert f'data-drop-assignee="{tenant_a.tech.user.pk}"' in body
    assert "data-drop-unassigned" in body
    assert "2 / 8 h" in body  # carga del lunes frente a la capacidad diaria
    assert "Carga vs. capacidad" not in body  # la tabla se reemplaza por las filas
    plain = client.get(reverse("work_order_board") + f"?week={next_monday().isoformat()}").content.decode()
    assert "data-drop-assignee" not in plain and "Carga vs. capacidad" in plain


def test_drop_on_technician_cell_sets_day_and_assignee(client, tenant_a, django_capture_on_commit_callbacks):
    with tenant_context(tenant_a.org):
        wo = _wo("x", asset=tenant_a.asset)
    login(client, tenant_a.admin)
    day = next_monday() + datetime.timedelta(days=2)
    with django_capture_on_commit_callbacks(execute=True):
        response = client.post(
            reverse("work_order_schedule", args=[wo.pk]),
            {"scheduled_for": day.isoformat(), "assigned_to": tenant_a.tech.user.pk},
            HTTP_X_BOARD_DROP="1",
        )
    assert response.status_code == 204
    with tenant_context(tenant_a.org):
        wo.refresh_from_db()
        assert (wo.status, wo.scheduled_for, wo.assigned_to) == (S.SCHEDULED, day, tenant_a.tech.user)
        assert Notification.objects.filter(recipient=tenant_a.tech.user).exists()


def test_lanes_no_js_form_returns_to_lanes(client, tenant_a):
    with tenant_context(tenant_a.org):
        wo = _wo("x", asset=tenant_a.asset)
    login(client, tenant_a.admin)
    day = next_monday()
    board = reverse("work_order_board") + f"?week={day.isoformat()}&lanes=1"
    assert f'name="next" value="{board.replace("&", "&amp;")}"' in client.get(board).content.decode()
    response = client.post(
        reverse("work_order_schedule", args=[wo.pk]),
        {"scheduled_for": day.isoformat(), "assigned_to": tenant_a.tech.user.pk, "next": board},
    )
    assert response.status_code == 302 and response.url == board
    with tenant_context(tenant_a.org):
        wo.refresh_from_db()
    assert (wo.scheduled_for, wo.assigned_to) == (day, tenant_a.tech.user)
