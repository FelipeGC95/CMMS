"""Servicios y selectores del modo sin conexión del técnico (X-06, ADR-011, paquete P1).

Hora informada por el cliente (`client_time`), regla «gana la última respuesta»,
fotos que llegan con la OT ya terminada, horas idempotentes y el snapshot de OT.
"""

import datetime
import uuid

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone

from apps.accounts.models import Membership, MembershipLocation, User
from apps.assets.models import Asset, Meter, MeterKind
from apps.attachments.models import Attachment
from apps.audit.models import AuditEvent
from apps.core.tenancy import tenant_context
from apps.maintenance.models import ItemType, PlanAssignment
from apps.maintenance.services import assign_plan, generate_due_work_orders
from apps.organizations.models import Location
from apps.work import selectors
from apps.work.models import FailureCode, FailureEvent, LaborEntry, WorkOrder
from apps.work.selectors import field_reference, field_work_orders
from apps.work.services import (
    CLIENT_TIME_TOLERANCE,
    WorkOrderError,
    add_checklist_item,
    add_checklist_photo,
    add_work_order_photo,
    answer_checklist_item,
    client_time,
    complete_work_order,
    create_work_order,
    log_labor,
    report_failure,
    resolve_client_time,
    save_checklist_note,
    transition,
)
from tests.conftest import tiny_jpeg_bytes

S = WorkOrder.Status


@pytest.fixture(autouse=True)
def _media(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path


def _photo():
    return SimpleUploadedFile("f.jpg", tiny_jpeg_bytes(), content_type="image/jpeg")


def _wo(tenant, *, created_ago=datetime.timedelta(hours=5), **kwargs):
    """OT correctiva planificada, «creada» hace `created_ago` (para probar horas del pasado)."""
    kwargs.setdefault("asset", tenant.asset)
    wo = create_work_order(work_type="corrective", title="OT", **kwargs)
    WorkOrder.objects.filter(pk=wo.pk).update(created_at=timezone.now() - created_ago)
    wo.refresh_from_db()
    return wo


def _events(wo, action=AuditEvent.Action.CHECKLIST_ANSWER):
    return list(AuditEvent.objects.filter(target_id=str(wo.pk), action=action).order_by("created_at"))


# --- client_time ---------------------------------------------------------------


def test_client_time_bounds_what_the_phone_says(db):
    now = timezone.now()
    created = now - datetime.timedelta(hours=3)
    assert abs(client_time(None) - now) < datetime.timedelta(seconds=5)
    two_hours_ago = now - datetime.timedelta(hours=2)
    assert resolve_client_time(two_hours_ago, not_before=created) == (two_hours_ago, "")
    # Futuro: ahora; dentro de la tolerancia del reloj no se marca.
    used, reason = resolve_client_time(now + datetime.timedelta(hours=1))
    assert reason == "future" and used <= timezone.now()
    used, reason = resolve_client_time(now + CLIENT_TIME_TOLERANCE / 2)
    assert reason == "" and used <= timezone.now()
    # Antes de la creación del objeto: la creación.
    assert resolve_client_time(now - datetime.timedelta(hours=4), not_before=created) == (
        created,
        "before_creation",
    )
    # Más de 30 días atrás: ahora, marcado.
    used, reason = resolve_client_time(now - datetime.timedelta(days=31), not_before=created)
    assert reason == "stale" and abs(used - now) < datetime.timedelta(seconds=5)


# --- Transiciones y término con la hora real -------------------------------------


def test_transition_uses_client_time_and_audits_it(in_a):
    wo = _wo(in_a)
    at = timezone.now() - datetime.timedelta(hours=2)
    transition(work_order=wo, to_status=S.IN_PROGRESS, membership=in_a.tech, at=at)
    wo.refresh_from_db()
    assert wo.started_at == at
    (event,) = AuditEvent.objects.filter(target_id=str(wo.pk), action=AuditEvent.Action.TRANSITION)
    assert event.changes["client_at"] == at.isoformat()
    assert "client_at_adjusted" not in event.changes


def test_transition_before_creation_is_clamped_and_marked(in_a):
    wo = _wo(in_a, created_ago=datetime.timedelta(hours=1))
    transition(
        work_order=wo,
        to_status=S.IN_PROGRESS,
        membership=in_a.tech,
        at=timezone.now() - datetime.timedelta(days=2),
    )
    wo.refresh_from_db()
    assert wo.started_at == wo.created_at
    event = AuditEvent.objects.get(target_id=str(wo.pk), action=AuditEvent.Action.TRANSITION)
    assert event.changes["client_at_adjusted"]["reason"] == "before_creation"


def test_stale_and_future_times_become_now(in_a):
    wo = _wo(in_a, created_ago=datetime.timedelta(days=40))
    before = timezone.now()
    transition(
        work_order=wo,
        to_status=S.IN_PROGRESS,
        membership=in_a.tech,
        at=timezone.now() - datetime.timedelta(days=35),
    )
    wo.refresh_from_db()
    assert wo.started_at >= before
    transition(
        work_order=wo,
        to_status=S.ON_HOLD,
        membership=in_a.tech,
        at=timezone.now() + datetime.timedelta(days=1),
    )
    reasons = [
        e.changes.get("client_at_adjusted", {}).get("reason")
        for e in AuditEvent.objects.filter(target_id=str(wo.pk), action=AuditEvent.Action.TRANSITION)
    ]
    assert sorted(reasons) == ["future", "stale"]


def test_complete_with_past_time_sets_start_end_close_and_plan_date(in_a):
    with tenant_context(in_a.org):
        assignment = assign_plan(plan=in_a.plan, asset=in_a.asset, starts_on=timezone.localdate())
        (wo,) = generate_due_work_orders()
    WorkOrder.objects.filter(pk=wo.pk).update(created_at=timezone.now() - datetime.timedelta(days=3))
    wo.refresh_from_db()
    for item in wo.checklist.all():
        item.result = "ok"
        item.save()
    at = timezone.now() - datetime.timedelta(days=2)
    wo = complete_work_order(work_order=wo, membership=in_a.tech, labor_minutes=30, at=at)
    wo.refresh_from_db()
    assert wo.started_at == at and wo.completed_at == at
    assert wo.status == S.CLOSED and wo.closed_at == at  # la organización no exige aprobación
    assignment = PlanAssignment.objects.get(pk=assignment.pk)
    assert assignment.last_done_on == timezone.localdate(at)


def test_complete_never_ends_before_the_start(in_a):
    wo = _wo(in_a)
    started = timezone.now() - datetime.timedelta(hours=1)
    transition(work_order=wo, to_status=S.IN_PROGRESS, membership=in_a.tech, at=started)
    wo.refresh_from_db()
    complete_work_order(
        work_order=wo, membership=in_a.tech, labor_minutes=0, at=started - datetime.timedelta(hours=2)
    )
    wo.refresh_from_db()
    assert wo.completed_at == started
    failure = FailureEvent.objects.get(work_order=wo)
    assert failure.ended_at == started


def test_complete_without_time_keeps_the_current_behaviour(in_a):
    wo = _wo(in_a)
    before = timezone.now()
    complete_work_order(work_order=wo, membership=in_a.tech, labor_minutes=10)
    wo.refresh_from_db()
    assert wo.completed_at >= before and wo.started_at >= before


def test_report_failure_with_reported_time_starts_the_failure_then(in_a):
    at = timezone.now() - datetime.timedelta(hours=3)
    request = report_failure(
        membership=in_a.tech, asset=in_a.asset, description="Fuga", asset_stopped=True, reported_at=at
    )
    failure = FailureEvent.objects.get(work_order=request.work_order)
    assert failure.started_at == at
    event = AuditEvent.objects.get(target_id=str(request.pk), action=AuditEvent.Action.CREATE)
    assert event.changes["client_at"] == at.isoformat()


def test_locked_and_cancelled_errors_carry_a_code(in_a):
    wo = _wo(in_a)
    transition(work_order=wo, to_status=S.CANCELLED, membership=in_a.admin)
    with pytest.raises(WorkOrderError) as error:
        transition(work_order=wo, to_status=S.IN_PROGRESS, membership=in_a.tech)
    assert error.value.code == "cancelled"
    done = _wo(in_a)
    complete_work_order(work_order=done, membership=in_a.tech, labor_minutes=0)
    done.refresh_from_db()
    with pytest.raises(WorkOrderError) as error:
        transition(work_order=done, to_status=S.IN_PROGRESS, membership=in_a.tech)
    assert error.value.code == "locked"
    with pytest.raises(WorkOrderError) as error:
        log_labor(work_order=done, membership=in_a.tech, minutes=5)
    assert error.value.code == "locked"


# --- Checklist: gana la última por answered_at -----------------------------------


def _item(tenant, wo, item_type=ItemType.CHECK, **kwargs):
    return add_checklist_item(
        work_order=wo, membership=tenant.admin, text=f"Ítem {item_type}", item_type=item_type, **kwargs
    )


def test_newer_answer_wins_and_older_is_discarded_and_audited(in_a):
    wo = _wo(in_a)
    item = _item(in_a, wo)
    newer = timezone.now() - datetime.timedelta(hours=1)
    older = timezone.now() - datetime.timedelta(hours=2)
    item = answer_checklist_item(item=item, membership=in_a.tech, result="ok", answered_at=newer)
    assert item.superseded is False and item.answered_at == newer
    assert _events(wo) == []  # primera respuesta: nada que auditar

    late = answer_checklist_item(
        item=item, membership=in_a.tech, result="not_ok", note="viejo", answered_at=older
    )
    assert late.superseded is True
    item.refresh_from_db()
    assert (item.result, item.note, item.answered_at) == ("ok", "", newer)
    (event,) = _events(wo)
    assert event.changes["discarded"]["result"] == "not_ok"
    assert event.changes["discarded"]["answered_at"] == older.isoformat()
    assert event.changes["kept_answered_at"] == newer.isoformat()
    assert event.changes["item_id"] == str(item.pk)


def test_overwriting_a_different_answer_is_audited_with_old_and_new(in_a):
    wo = _wo(in_a)
    item = _item(in_a, wo)
    answer_checklist_item(
        item=item, membership=in_a.tech, result="ok", answered_at=timezone.now() - datetime.timedelta(hours=1)
    )
    item = answer_checklist_item(item=item, membership=in_a.admin, result="not_ok", note="fuga")  # web: ahora
    assert item.superseded is False and item.result == "not_ok"
    (event,) = _events(wo)
    assert event.changes["result"] == ["ok", "not_ok"]
    assert event.changes["note"] == ["", "fuga"]
    assert event.actor == in_a.admin.user
    # Repetir la misma respuesta no deja otro evento.
    answer_checklist_item(item=item, membership=in_a.admin, result="not_ok", note="fuga")
    assert len(_events(wo)) == 1


def test_measurement_answer_order_uses_answered_at(in_a):
    wo = _wo(in_a)
    item = _item(in_a, wo, ItemType.MEASUREMENT, unit="bar", min_value=1, max_value=5)
    answer_checklist_item(
        item=item,
        membership=in_a.tech,
        value_number="3",
        answered_at=timezone.now() - datetime.timedelta(minutes=5),
    )
    late = answer_checklist_item(
        item=item,
        membership=in_a.tech,
        value_number="9",
        answered_at=timezone.now() - datetime.timedelta(hours=1),
    )
    assert late.superseded and late.value_number == 3


def test_first_offline_answer_starts_the_work_order_at_that_time(in_a):
    wo = _wo(in_a)
    item = _item(in_a, wo)
    at = timezone.now() - datetime.timedelta(hours=2)
    answer_checklist_item(item=item, membership=in_a.tech, result="ok", answered_at=at)
    wo.refresh_from_db()
    assert wo.status == S.IN_PROGRESS and wo.started_at == at


def test_note_follows_the_same_rule(in_a):
    wo = _wo(in_a)
    item = _item(in_a, wo)
    answered = timezone.now() - datetime.timedelta(hours=1)
    answer_checklist_item(item=item, membership=in_a.tech, result="ok", note="a", answered_at=answered)
    late = save_checklist_note(
        item=item, membership=in_a.tech, note="vieja", answered_at=answered - datetime.timedelta(minutes=1)
    )
    assert late.superseded
    item.refresh_from_db()
    assert item.note == "a"
    assert _events(wo)[-1].changes["discarded"] == {
        "note": "vieja",
        "answered_at": (answered - datetime.timedelta(minutes=1)).isoformat(),
    }
    newer = save_checklist_note(item=item, membership=in_a.tech, note="nueva")
    assert not newer.superseded
    item.refresh_from_db()
    assert item.note == "nueva" and item.answered_at > answered
    assert _events(wo)[-1].changes["note"] == ["a", "nueva"]


# --- Fotos tomadas sin conexión ------------------------------------------------


def test_photo_taken_before_completion_is_accepted_on_a_closed_work_order(in_a):
    wo = _wo(in_a)
    item = _item(in_a, wo, ItemType.PHOTO)
    at = timezone.now() - datetime.timedelta(hours=2)
    add_checklist_photo(item=item, membership=in_a.tech, uploaded_file=_photo(), taken_at=at)
    item.refresh_from_db()
    assert item.result == "done" and item.answered_at == at
    complete_work_order(work_order=wo, membership=in_a.tech, labor_minutes=0)
    wo.refresh_from_db()
    assert wo.status == S.CLOSED
    taken = timezone.now() - datetime.timedelta(hours=1)
    add_checklist_photo(item=item, membership=in_a.tech, uploaded_file=_photo(), taken_at=taken)
    add_work_order_photo(work_order=wo, membership=in_a.tech, uploaded_file=_photo(), taken_at=taken)
    assert Attachment.objects.filter(target_id__in=[wo.pk, item.pk]).count() == 3
    item.refresh_from_db()
    assert item.answered_at == at  # el resultado del ítem no cambia

    for taken_at in (None, timezone.now() + datetime.timedelta(hours=1)):
        with pytest.raises(WorkOrderError) as error:
            add_work_order_photo(
                work_order=wo, membership=in_a.tech, uploaded_file=_photo(), taken_at=taken_at
            )
        assert error.value.code == "locked"


def test_photo_on_a_cancelled_work_order_is_rejected(in_a):
    wo = _wo(in_a)
    item = _item(in_a, wo)
    transition(work_order=wo, to_status=S.CANCELLED, membership=in_a.admin)
    wo.refresh_from_db()
    taken = timezone.now() - datetime.timedelta(hours=1)
    with pytest.raises(WorkOrderError) as error:
        add_work_order_photo(work_order=wo, membership=in_a.tech, uploaded_file=_photo(), taken_at=taken)
    assert error.value.code == "cancelled"
    item.refresh_from_db()
    with pytest.raises(WorkOrderError) as error:
        add_checklist_photo(item=item, membership=in_a.tech, uploaded_file=_photo(), taken_at=taken)
    assert error.value.code == "cancelled"
    assert not Attachment.objects.filter(target_id__in=[wo.pk, item.pk]).exists()


# --- Horas idempotentes ----------------------------------------------------------


def test_log_labor_is_idempotent_by_client_uuid(in_a):
    wo = _wo(in_a)
    key = uuid.uuid4()
    first = log_labor(work_order=wo, membership=in_a.tech, minutes=45, client_uuid=key)
    again = log_labor(work_order=wo, membership=in_a.tech, minutes=45, client_uuid=key)
    assert first.pk == again.pk
    assert LaborEntry.objects.filter(work_order=wo).count() == 1
    assert AuditEvent.objects.filter(target_id=str(wo.pk), action=AuditEvent.Action.COST_ADD).count() == 1
    # Reenvío tras cerrar la OT: devuelve lo ya registrado.
    complete_work_order(work_order=wo, membership=in_a.tech, labor_minutes=0)
    assert log_labor(work_order=wo, membership=in_a.tech, minutes=45, client_uuid=key).pk == first.pk
    # La misma clave en otra OT es un conflicto.
    other = _wo(in_a)
    with pytest.raises(WorkOrderError) as error:
        log_labor(work_order=other, membership=in_a.tech, minutes=10, client_uuid=key)
    assert error.value.code == "conflict"


# --- Snapshot de OT y catálogos ---------------------------------------------------


def _scoped_tech(tenant, location):
    user = User.objects.create_user(f"scoped@{tenant.org.slug}.cl", "x", full_name="Técnico con alcance")
    member = Membership.objects.create(
        user=user, organization=tenant.org, role="technician", restricted_to_locations=True
    )
    MembershipLocation.objects.create(membership=member, location=location)
    return member


def test_field_work_orders_takes_every_visible_open_work_order_mine_first(in_a):
    today = timezone.localdate()
    mine = _wo(in_a, assigned_to=in_a.tech.user, due_on=today + datetime.timedelta(days=5))
    helping = _wo(in_a, helpers=[in_a.tech.user], due_on=today + datetime.timedelta(days=6))
    unassigned = _wo(in_a, due_on=today)
    overdue = _wo(in_a, assigned_to=in_a.admin.user, due_on=today - datetime.timedelta(days=10))
    undated = _wo(in_a)
    scheduled = _wo(in_a, status=S.SCHEDULED)
    WorkOrder.objects.filter(pk=scheduled.pk).update(scheduled_for=today + datetime.timedelta(days=1))
    running = _wo(in_a, due_on=today + datetime.timedelta(days=60))
    transition(work_order=running, to_status=S.IN_PROGRESS, membership=in_a.admin)
    later = _wo(in_a, due_on=today + datetime.timedelta(days=10))
    done = _wo(in_a, due_on=today)
    complete_work_order(work_order=done, membership=in_a.tech, labor_minutes=0)

    snapshot = field_work_orders(membership=in_a.tech, days=7)
    ids = [wo.pk for wo in snapshot.work_orders]
    assert set(ids) == {mine.pk, helping.pk, unassigned.pk, overdue.pk, undated.pk, scheduled.pk, running.pk}
    assert ids[:2] == [mine.pk, helping.pk]  # asignadas primero (responsable o apoyo), por fecha
    assert later.pk not in ids and done.pk not in ids
    assert not snapshot.truncated
    flags = {wo.pk: wo.assigned_to_me for wo in snapshot.work_orders}
    assert flags[mine.pk] and flags[helping.pk] and not flags[unassigned.pk]
    # El horizonte se acota a 14 días.
    assert field_work_orders(membership=in_a.tech, days=99).horizon == today + datetime.timedelta(days=14)
    assert later.pk in {wo.pk for wo in field_work_orders(membership=in_a.tech, days=99).work_orders}


def test_field_work_orders_respects_site_scope(in_a):
    site = Location.objects.create(code="S1", name="Planta 1")
    other = Location.objects.create(code="S2", name="Planta 2")
    inside = Asset.objects.create(code="IN", name="Dentro", asset_class=in_a.asset.asset_class, location=site)
    outside = Asset.objects.create(
        code="OUT", name="Fuera", asset_class=in_a.asset.asset_class, location=other
    )
    seen = _wo(in_a, asset=inside)
    at_site = _wo(in_a, asset=None, location=site)
    hidden = _wo(in_a, asset=outside, assigned_to=in_a.tech.user)
    member = _scoped_tech(in_a, site)
    ids = {wo.pk for wo in field_work_orders(membership=member, days=7).work_orders}
    assert ids == {seen.pk, at_site.pk}
    assert hidden.pk not in ids
    reference = field_reference(membership=member)
    assert {a["id"] for a in reference.assets} == {inside.pk}


def test_field_work_orders_is_capped_and_says_so(in_a, monkeypatch):
    monkeypatch.setattr(selectors, "FIELD_WORK_ORDER_LIMIT", 2)
    for _i in range(3):
        _wo(in_a)
    snapshot = field_work_orders(membership=in_a.tech)
    assert len(snapshot.work_orders) == 2 and snapshot.truncated


def test_field_work_orders_bring_checklist_photos_and_meter(in_a):
    meter = Meter.objects.create(asset=in_a.asset, kind=MeterKind.HOURS)
    wo = _wo(in_a)
    item = _item(in_a, wo, ItemType.PHOTO)
    add_checklist_photo(item=item, membership=in_a.tech, uploaded_file=_photo())
    add_checklist_photo(item=item, membership=in_a.tech, uploaded_file=_photo())
    (row,) = field_work_orders(membership=in_a.tech).work_orders
    (snap_item,) = row.field_checklist
    assert snap_item.photo_count == 2 and snap_item.result == "done"
    assert row.field_meter == meter and row.meter_required is False


def test_field_reference_lists_active_codes_assets_and_meters(in_a):
    meter = Meter.objects.create(asset=in_a.asset, kind=MeterKind.HOURS)
    Meter.objects.create(asset=in_a.asset, kind=MeterKind.CYCLES, is_active=False)
    FailureCode.objects.create(kind=FailureCode.Kind.MODE, code="ZZZ", name="Inactivo", is_active=False)
    FailureCode.objects.create(kind=FailureCode.Kind.MODE, code="AAA", name="Activo")
    retired = Asset.objects.create(
        code="OLD", name="Viejo", asset_class=in_a.asset.asset_class, is_active=False
    )
    reference = field_reference(membership=in_a.tech)
    modes = [c["code"] for c in reference.failure_codes[FailureCode.Kind.MODE]]
    assert "AAA" in modes and "ZZZ" not in modes
    assert {a["id"] for a in reference.assets} == {in_a.asset.pk}
    assert retired.pk not in {a["id"] for a in reference.assets}
    assert [m["id"] for m in reference.meters] == [meter.pk]
    assert not reference.assets_truncated
