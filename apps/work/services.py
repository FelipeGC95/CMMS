"""Reglas de negocio de OT, avisos de falla y checklist."""

import datetime
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from apps.accounts.permissions import Perm, has_perm, require_perm
from apps.assets.models import Asset, MeterReading
from apps.assets.services import change_asset_status, record_meter_reading

from .models import (
    Attachment,
    FailureEvent,
    LaborEntry,
    WorkOrder,
    WorkOrderChecklistItem,
    WorkOrderSequence,
    WorkOrderTransition,
    WorkRequest,
)

S = WorkOrder.Status

# Ciclo de vida fijo con pasos opcionales (ADR-008).
TRANSITIONS: dict[str, set[str]] = {
    S.REQUESTED: {S.PLANNED, S.SCHEDULED, S.IN_PROGRESS, S.CANCELLED},
    S.PLANNED: {S.SCHEDULED, S.IN_PROGRESS, S.CANCELLED},
    S.SCHEDULED: {S.PLANNED, S.IN_PROGRESS, S.CANCELLED},
    S.IN_PROGRESS: {S.ON_HOLD, S.COMPLETED, S.CANCELLED},
    S.ON_HOLD: {S.IN_PROGRESS, S.CANCELLED},
    S.COMPLETED: {S.CLOSED, S.IN_PROGRESS},
    S.CLOSED: set(),
    S.CANCELLED: set(),
}

# Transiciones que un técnico puede hacer; el resto requiere WORK_ORDER_MANAGE.
EXECUTION_TARGETS = {S.IN_PROGRESS, S.ON_HOLD, S.COMPLETED}

ALLOWED_UPLOAD_TYPES = {"image/jpeg", "image/png", "image/webp", "image/heic", "application/pdf"}


class WorkOrderError(ValidationError):
    pass


def next_number(*, key: str) -> int:
    """Siguiente correlativo, con bloqueo de fila (sin huecos por concurrencia)."""
    sequence, _ = WorkOrderSequence.objects.select_for_update().get_or_create(key=key)
    sequence.last_number += 1
    sequence.save(update_fields=["last_number", "updated_at"])
    return sequence.last_number


@transaction.atomic
def create_work_order(
    *,
    work_type: str,
    title: str,
    asset: Asset,
    description: str = "",
    priority: int = WorkOrder.Priority.NORMAL,
    status: str = S.PLANNED,
    due_on: datetime.date | None = None,
    due_meter_value=None,
    estimated_minutes: int | None = None,
    assigned_to=None,
    plan_assignment=None,
    plan_version=None,
    checklist=(),
    by=None,
    client_uuid=None,
) -> WorkOrder:
    work_order = WorkOrder.objects.create(
        number=next_number(key=work_type),
        work_type=work_type,
        status=status,
        priority=priority,
        title=title[:200],
        description=description,
        asset=asset,
        due_on=due_on,
        due_meter_value=due_meter_value,
        estimated_minutes=estimated_minutes,
        assigned_to=assigned_to,
        plan_assignment=plan_assignment,
        plan_version=plan_version,
        client_uuid=client_uuid,
    )
    # Snapshot del checklist: cambiar el plan después no altera esta OT.
    WorkOrderChecklistItem.objects.bulk_create(
        [
            WorkOrderChecklistItem(
                organization=work_order.organization,
                work_order=work_order,
                order=item.order,
                text=item.text,
                item_type=item.item_type,
                unit=item.unit,
                min_value=item.min_value,
                max_value=item.max_value,
                required=item.required,
            )
            for item in checklist
        ]
    )
    WorkOrderTransition.objects.create(work_order=work_order, to_status=status, by=by)
    return work_order


def _suggest_priority(asset: Asset, asset_stopped: bool) -> int:
    if asset_stopped and asset.criticality == Asset.Criticality.A:
        return WorkOrder.Priority.URGENT
    if asset_stopped:
        return WorkOrder.Priority.HIGH
    return WorkOrder.Priority.NORMAL


@transaction.atomic
def report_failure(
    *,
    membership,
    asset: Asset,
    description: str,
    asset_stopped: bool,
    photo=None,
    client_uuid=None,
) -> WorkRequest:
    """Aviso de falla (REQ-01): crea la solicitud y su OT correctiva en un paso.

    Idempotente por `client_uuid` (reintentos desde móvil no duplican).
    """
    require_perm(membership, Perm.REQUEST_CREATE)
    if client_uuid:
        existing = WorkRequest.objects.filter(client_uuid=client_uuid).first()
        if existing:
            return existing

    user = membership.user
    now = timezone.now()
    work_order = create_work_order(
        work_type=WorkOrder.Type.CORRECTIVE,
        title=description.strip().splitlines()[0][:120] or "Falla reportada",
        description=description,
        asset=asset,
        priority=_suggest_priority(asset, asset_stopped),
        status=S.REQUESTED,
        due_on=timezone.localdate() if asset_stopped else None,
        by=user,
    )
    request = WorkRequest.objects.create(
        number=next_number(key="request"),
        asset=asset,
        description=description,
        asset_stopped=asset_stopped,
        requested_by=user,
        work_order=work_order,
        client_uuid=client_uuid,
    )
    if asset_stopped:
        FailureEvent.objects.create(
            asset=asset,
            work_order=work_order,
            impact=FailureEvent.Impact.TOTAL_STOP,
            description=description,
            started_at=now,
        )
        change_asset_status(asset=asset, status=Asset.Status.STOPPED, by=user, reason=work_order.code, at=now)
    if photo is not None:
        add_attachment(work_order=work_order, uploaded_file=photo, by=user)
    return request


def add_attachment(*, work_order: WorkOrder, uploaded_file, by) -> Attachment:
    if uploaded_file.size > settings.MAX_UPLOAD_BYTES:
        raise WorkOrderError("El archivo supera el tamaño máximo (10 MB).")
    if uploaded_file.content_type not in ALLOWED_UPLOAD_TYPES:
        raise WorkOrderError("Tipo de archivo no permitido.")
    return Attachment.objects.create(
        work_order=work_order,
        file=uploaded_file,
        filename=uploaded_file.name[:200],
        content_type=uploaded_file.content_type,
        size_bytes=uploaded_file.size,
        uploaded_by=by,
    )


def allowed_transitions(*, work_order: WorkOrder, membership) -> list[str]:
    targets = TRANSITIONS[work_order.status]
    if not has_perm(membership, Perm.WORK_ORDER_MANAGE):
        if not has_perm(membership, Perm.WORK_ORDER_EXECUTE):
            return []
        targets = targets & EXECUTION_TARGETS
    return [s for s in S.values if s in targets]


@transaction.atomic
def transition(*, work_order: WorkOrder, to_status: str, membership, comment: str = "") -> WorkOrder:
    if to_status not in allowed_transitions(work_order=work_order, membership=membership):
        raise WorkOrderError(f"Transición no permitida: {work_order.get_status_display()} → {to_status}.")
    if to_status == S.COMPLETED:
        pending = work_order.checklist.filter(required=True, result="").count()
        if pending:
            raise WorkOrderError(f"Faltan {pending} ítems obligatorios del checklist.")

    user = membership.user
    now = timezone.now()
    from_status = work_order.status
    work_order.status = to_status
    if to_status == S.IN_PROGRESS:
        work_order.started_at = work_order.started_at or now
        if work_order.assigned_to_id is None:
            work_order.assigned_to = user
        if work_order.asset.status == Asset.Status.STOPPED:
            change_asset_status(
                asset=work_order.asset,
                status=Asset.Status.IN_MAINTENANCE,
                by=user,
                reason=work_order.code,
                at=now,
            )
    elif to_status == S.COMPLETED:
        work_order.completed_at = now
    work_order.save()
    WorkOrderTransition.objects.create(
        work_order=work_order, from_status=from_status, to_status=to_status, by=user, comment=comment
    )

    if to_status == S.COMPLETED and not work_order.organization.require_close_approval:
        _close(work_order=work_order, user=user, at=now)
    elif to_status == S.CLOSED:
        _close(work_order=work_order, user=user, at=now, record=False)
    return work_order


def _close(*, work_order: WorkOrder, user, at, record=True):
    from apps.maintenance.services import register_completion

    if record:
        WorkOrderTransition.objects.create(
            work_order=work_order,
            from_status=work_order.status,
            to_status=S.CLOSED,
            by=user,
            comment="Cierre automático (la organización no exige aprobación)",
        )
    work_order.status = S.CLOSED
    work_order.closed_at = at
    work_order.save(update_fields=["status", "closed_at", "updated_at"])

    failure = FailureEvent.objects.filter(work_order=work_order, ended_at__isnull=True).first()
    if failure:
        failure.ended_at = at
        failure.save(update_fields=["ended_at", "updated_at"])

    asset = work_order.asset
    still_down = FailureEvent.objects.filter(asset=asset, ended_at__isnull=True).exists()
    if asset.status in (Asset.Status.STOPPED, Asset.Status.IN_MAINTENANCE) and not still_down:
        change_asset_status(
            asset=asset, status=Asset.Status.OPERATIONAL, by=user, reason=work_order.code, at=at
        )
    if work_order.plan_assignment_id:
        register_completion(
            assignment=work_order.plan_assignment,
            completed_on=timezone.localdate(at),
            meter_value=work_order.meter_value_at_completion,
        )


@transaction.atomic
def answer_checklist_item(
    *, item: WorkOrderChecklistItem, membership, result: str = "", value_number=None, note=""
) -> WorkOrderChecklistItem:
    """Registra la respuesta de un ítem. La primera respuesta inicia la OT."""
    require_perm(membership, Perm.WORK_ORDER_EXECUTE)
    work_order = item.work_order
    if work_order.status in (S.COMPLETED, *WorkOrder.CLOSED_STATUSES):
        raise WorkOrderError("La OT ya no admite cambios.")
    if work_order.status != S.IN_PROGRESS:
        transition(work_order=work_order, to_status=S.IN_PROGRESS, membership=membership)

    if item.item_type == "measurement":
        if value_number in (None, ""):
            raise WorkOrderError("Ingresa el valor medido.")
        value = Decimal(str(value_number))
        out_of_range = (item.min_value is not None and value < item.min_value) or (
            item.max_value is not None and value > item.max_value
        )
        item.value_number = value
        result = item.Result.NOT_OK if out_of_range else item.Result.OK
    elif result not in item.Result.values:
        raise WorkOrderError("Resultado inválido.")

    item.result = result
    item.note = note[:500]
    item.answered_by = membership.user
    item.answered_at = timezone.now()
    item.save()
    return item


def work_order_meter(work_order: WorkOrder):
    """Medidor cuya lectura se pide al cerrar: el del plan o, si no, el primero del activo."""
    if work_order.plan_assignment_id and work_order.plan_assignment.meter_id:
        return work_order.plan_assignment.meter
    return work_order.asset.meters.filter(is_active=True).order_by("kind").first()


@transaction.atomic
def complete_work_order(
    *,
    work_order: WorkOrder,
    membership,
    labor_minutes: int,
    notes: str = "",
    failure_mode=None,
    failure_cause=None,
    meter_value=None,
) -> WorkOrder:
    """Completa la OT; si se informa la lectura del medidor, la registra y reprograma desde ella."""
    if meter_value is not None:
        meter = work_order_meter(work_order)
        if meter is not None:
            reading, _ = record_meter_reading(
                meter=meter,
                value=meter_value,
                membership=membership,
                source=MeterReading.Source.WORK_ORDER,
                work_order=work_order,
                generate_work_orders=False,
            )
            work_order.meter_value_at_completion = reading.value
            work_order.save(update_fields=["meter_value_at_completion", "updated_at"])
    if labor_minutes:
        LaborEntry.objects.create(work_order=work_order, user=membership.user, minutes=labor_minutes)
    work_order.completion_notes = notes
    work_order.save(update_fields=["completion_notes", "updated_at"])
    failure = FailureEvent.objects.filter(work_order=work_order).first()
    if failure and (failure_mode or failure_cause):
        failure.failure_mode = failure_mode
        failure.failure_cause = failure_cause
        failure.save(update_fields=["failure_mode", "failure_cause", "updated_at"])
    if work_order.status != S.IN_PROGRESS:
        transition(work_order=work_order, to_status=S.IN_PROGRESS, membership=membership)
    return transition(work_order=work_order, to_status=S.COMPLETED, membership=membership)
