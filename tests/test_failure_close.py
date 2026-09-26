"""Cierre de correctiva con registro de falla (REQ-03 CA1–CA3) y observación por ítem (WO-03 CA2)."""

import datetime

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.accounts.permissions import PermissionDeniedError
from apps.assets.models import Asset, AssetStatusPeriod
from apps.audit.models import AuditEvent
from apps.work.models import FailureCode, FailureEvent, WorkOrder
from apps.work.services import (
    WorkOrderError,
    answer_checklist_item,
    complete_work_order,
    create_work_order,
    report_failure,
    save_checklist_note,
)
from tests.conftest import login

Impact = FailureEvent.Impact


def _corrective(tenant, description="Ruido en el rodamiento"):
    """Correctiva avisada SIN "equipo detenido": todavía no tiene evento de falla."""
    return report_failure(
        membership=tenant.viewer, asset=tenant.asset, description=description, asset_stopped=False
    ).work_order


# --- Servicio: toda correctiva cerrada deja su falla (CA2) -------------------------


def test_completing_a_corrective_without_stop_creates_the_failure_event(in_a):
    wo = _corrective(in_a)
    assert not FailureEvent.objects.filter(work_order=wo).exists()
    periods_before = AssetStatusPeriod.objects.count()

    complete_work_order(work_order=wo, membership=in_a.tech, labor_minutes=20, notes="Cambio de rodamiento")

    failure = FailureEvent.objects.get(work_order=wo)
    assert failure.asset == in_a.asset
    assert failure.impact == Impact.NONE
    assert failure.description == "Ruido en el rodamiento"  # síntoma: por defecto, el aviso
    assert failure.ended_at is not None and failure.started_at <= failure.ended_at
    wo.refresh_from_db()
    assert wo.status == WorkOrder.Status.CLOSED
    assert wo.completion_notes == "Cambio de rodamiento"
    # Sin detención: el estado del activo no se toca.
    in_a.asset.refresh_from_db()
    assert in_a.asset.status == Asset.Status.OPERATIONAL
    assert AssetStatusPeriod.objects.count() == periods_before


def test_created_failure_event_is_audited(in_a):
    wo = _corrective(in_a)
    complete_work_order(work_order=wo, membership=in_a.tech, labor_minutes=5)
    failure = FailureEvent.objects.get(work_order=wo)
    event = AuditEvent.objects.get(target_id=failure.pk)
    assert event.action == AuditEvent.Action.CREATE


def test_completion_records_impact_times_symptom_and_action(in_a):
    wo = _corrective(in_a)
    start = timezone.now() - datetime.timedelta(hours=3)
    end = timezone.now() - datetime.timedelta(hours=1)
    complete_work_order(
        work_order=wo,
        membership=in_a.tech,
        labor_minutes=90,
        notes="Se reemplazó el rodamiento lado acople.",
        impact=Impact.PARTIAL,
        failure_started_at=start,
        failure_ended_at=end,
        symptom="Ruido metálico y temperatura alta en el motor.",
    )
    failure = FailureEvent.objects.get(work_order=wo)
    assert failure.impact == Impact.PARTIAL
    assert (failure.started_at, failure.ended_at) == (start, end)
    assert failure.description == "Ruido metálico y temperatura alta en el motor."
    wo.refresh_from_db()
    assert wo.completion_notes == "Se reemplazó el rodamiento lado acople."


def test_completion_updates_the_event_opened_by_the_report(in_a):
    wo = report_failure(
        membership=in_a.viewer, asset=in_a.asset, description="No parte", asset_stopped=True
    ).work_order
    complete_work_order(
        work_order=wo,
        membership=in_a.tech,
        labor_minutes=30,
        impact=Impact.PARTIAL,
        symptom="Contactor quemado",
    )
    assert FailureEvent.objects.filter(work_order=wo).count() == 1
    failure = FailureEvent.objects.get(work_order=wo)
    assert failure.impact == Impact.PARTIAL
    assert failure.description == "Contactor quemado"
    assert failure.ended_at is not None
    in_a.asset.refresh_from_db()
    assert in_a.asset.status == Asset.Status.OPERATIONAL


def test_end_of_stop_cannot_precede_its_start(in_a):
    wo = _corrective(in_a)
    now = timezone.now()
    with pytest.raises(WorkOrderError):
        complete_work_order(
            work_order=wo,
            membership=in_a.tech,
            labor_minutes=5,
            failure_started_at=now,
            failure_ended_at=now - datetime.timedelta(minutes=5),
        )
    assert not FailureEvent.objects.filter(work_order=wo).exists()


def test_invalid_impact_is_rejected(in_a):
    wo = _corrective(in_a)
    with pytest.raises(WorkOrderError):
        complete_work_order(work_order=wo, membership=in_a.tech, labor_minutes=5, impact="meltdown")


def test_required_failure_details_apply_to_correctives_without_a_stop(in_a):
    in_a.org.require_failure_details = True
    in_a.org.save()
    wo = _corrective(in_a)
    with pytest.raises(WorkOrderError):
        complete_work_order(work_order=wo, membership=in_a.tech, labor_minutes=5)
    mode = FailureCode.objects.filter(kind=FailureCode.Kind.MODE).first()
    cause = FailureCode.objects.filter(kind=FailureCode.Kind.CAUSE).first()
    complete_work_order(
        work_order=wo, membership=in_a.tech, labor_minutes=5, failure_mode=mode, failure_cause=cause
    )
    failure = FailureEvent.objects.get(work_order=wo)
    assert (failure.failure_mode, failure.failure_cause) == (mode, cause)


def test_non_corrective_work_orders_get_no_failure_event(in_a):
    wo = create_work_order(work_type="improvement", title="x", asset=in_a.asset)
    complete_work_order(work_order=wo, membership=in_a.tech, labor_minutes=5, impact=Impact.TOTAL_STOP)
    assert not FailureEvent.objects.filter(work_order=wo).exists()


def test_with_close_approval_the_new_event_is_already_ended(in_a):
    """Una OT completada a la espera de aprobación no deja el activo "caído" por su falla."""
    in_a.org.require_close_approval = True
    in_a.org.save()
    wo = _corrective(in_a)
    complete_work_order(work_order=wo, membership=in_a.tech, labor_minutes=5)
    assert wo.status == WorkOrder.Status.COMPLETED
    assert not FailureEvent.objects.filter(asset=in_a.asset, ended_at__isnull=True).exists()


# --- Servicio: observación por ítem (WO-03 CA2) -------------------------------------


def _preventive(tenant):
    return create_work_order(
        work_type="preventive", title="x", asset=tenant.asset, checklist=tenant.plan.items.all()
    )


def test_note_is_saved_without_touching_the_result(in_a):
    wo = _preventive(in_a)
    item = wo.checklist.first()
    answer_checklist_item(item=item, membership=in_a.tech, result="ok")
    save_checklist_note(item=item, membership=in_a.tech, note="  Purga con agua turbia  ")
    item.refresh_from_db()
    assert item.result == "ok" and item.note == "Purga con agua turbia"


def test_note_can_be_written_before_answering(in_a):
    wo = _preventive(in_a)
    item = wo.checklist.first()
    save_checklist_note(item=item, membership=in_a.tech, note="Revisar drenaje")
    item.refresh_from_db()
    assert item.result == "" and item.note == "Revisar drenaje"


def test_note_requires_execute_permission(in_a):
    item = _preventive(in_a).checklist.first()
    with pytest.raises(PermissionDeniedError):
        save_checklist_note(item=item, membership=in_a.viewer, note="x")


def test_note_is_rejected_once_the_work_order_is_done(in_a):
    wo = _preventive(in_a)
    WorkOrder.objects.filter(pk=wo.pk).update(status=WorkOrder.Status.CLOSED)
    item = wo.checklist.select_related("work_order").first()
    with pytest.raises(WorkOrderError):
        save_checklist_note(item=item, membership=in_a.tech, note="x")


# --- Vistas -------------------------------------------------------------------------


def test_completion_form_asks_symptom_and_action_for_correctives(client, in_a):
    wo = _corrective(in_a)
    login(client, in_a.tech)
    html = client.get(reverse("work_order_detail", args=[wo.pk])).content.decode()
    assert "Síntoma observado" in html and "Acción realizada" in html
    assert "¿Qué viste? ¿Qué hiciste?" in html
    assert 'name="impact"' in html and 'type="datetime-local"' in html
    assert "Comentario de cierre" not in html


def test_completion_form_of_a_preventive_has_no_failure_fields(client, in_a):
    wo = create_work_order(work_type="preventive", title="x", asset=in_a.asset)
    login(client, in_a.tech)
    html = client.get(reverse("work_order_detail", args=[wo.pk])).content.decode()
    assert "Comentario de cierre" in html
    assert "Síntoma observado" not in html and 'name="impact"' not in html


def test_complete_view_records_the_failure(client, in_a):
    wo = _corrective(in_a)
    login(client, in_a.tech)
    response = client.post(
        reverse("work_order_complete", args=[wo.pk]),
        {
            "labor_minutes": "40",
            "symptom": "Vibración fuerte",
            "notes": "Se alineó el acople",
            "impact": Impact.PARTIAL,
            "failure_started_at": "2026-09-20T08:00",
            "failure_ended_at": "2026-09-20T10:30",
        },
    )
    assert response.status_code == 302
    failure = FailureEvent.objects.get(work_order=wo)
    assert failure.impact == Impact.PARTIAL and failure.description == "Vibración fuerte"
    assert failure.ended_at - failure.started_at == datetime.timedelta(hours=2, minutes=30)
    wo.refresh_from_db()
    assert wo.completion_notes == "Se alineó el acople"
    detail = client.get(reverse("work_order_detail", args=[wo.pk])).content.decode()
    assert "Vibración fuerte" in detail and "Detención parcial" in detail


def test_complete_view_rejects_end_before_start(client, in_a):
    wo = _corrective(in_a)
    login(client, in_a.tech)
    response = client.post(
        reverse("work_order_complete", args=[wo.pk]),
        {
            "labor_minutes": "10",
            "impact": Impact.TOTAL_STOP,
            "failure_started_at": "2026-09-20T10:00",
            "failure_ended_at": "2026-09-20T09:00",
        },
    )
    assert response.status_code == 400
    assert not FailureEvent.objects.filter(work_order=wo).exists()


def test_checklist_note_autosave_view(client, in_a):
    wo = _preventive(in_a)
    item = wo.checklist.first()
    login(client, in_a.tech)
    response = client.post(
        reverse("checklist_answer", args=[wo.pk, item.pk]),
        {"note_only": "1", "note": "Filtro sucio"},
        HTTP_HX_REQUEST="true",
    )
    assert response.status_code == 200
    assert "Observación guardada" in response.content.decode()
    item.refresh_from_db()
    assert item.note == "Filtro sucio" and item.result == ""


def test_answering_keeps_the_note_sent_with_the_result(client, in_a):
    wo = _preventive(in_a)
    item = wo.checklist.first()
    login(client, in_a.tech)
    client.post(
        reverse("checklist_answer", args=[wo.pk, item.pk]),
        {"result": "not_ok", "note": "Válvula trabada"},
        HTTP_HX_REQUEST="true",
    )
    item.refresh_from_db()
    assert item.result == "not_ok" and item.note == "Válvula trabada"


@pytest.mark.parametrize(("role", "link"), [("admin", "work_order_create"), ("tech", "asset_report")])
def test_out_of_range_measurement_suggests_a_corrective(client, in_a, role, link):
    wo = _preventive(in_a)
    measure = wo.checklist.get(item_type="measurement")
    login(client, getattr(in_a, role))
    response = client.post(
        reverse("checklist_answer", args=[wo.pk, measure.pk]),
        {"value_number": "9.5"},
        HTTP_HX_REQUEST="true",
    )
    html = response.content.decode()
    assert "crea una OT correctiva" in html
    expected = reverse(link) if link == "work_order_create" else reverse(link, args=[in_a.asset.pk])
    assert expected in html


def test_in_range_measurement_has_no_corrective_hint(client, in_a):
    wo = _preventive(in_a)
    measure = wo.checklist.get(item_type="measurement")
    login(client, in_a.tech)
    response = client.post(
        reverse("checklist_answer", args=[wo.pk, measure.pk]),
        {"value_number": "7"},
        HTTP_HX_REQUEST="true",
    )
    assert "crea una OT correctiva" not in response.content.decode()


def test_create_view_is_prefilled_from_the_querystring(client, in_a):
    login(client, in_a.admin)
    url = reverse("work_order_create")
    response = client.get(url, {"work_type": "corrective", "asset": str(in_a.asset.pk), "title": "Presión"})
    form = response.context["form"]
    assert form.initial == {"work_type": "corrective", "asset": str(in_a.asset.pk), "title": "Presión"}
