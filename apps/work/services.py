"""Reglas de negocio de OT, avisos de falla y checklist."""

import datetime
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.translation import gettext as _

from apps.accounts.permissions import Perm, has_perm, require_perm
from apps.assets.models import Asset, MeterReading
from apps.assets.services import change_asset_status, record_meter_reading
from apps.attachments import services as attachments_services
from apps.attachments.models import Attachment
from apps.attachments.models import Purpose as AttachmentPurpose
from apps.attachments.models import TargetType as AttachmentTargetType
from apps.attachments.services import AttachmentError, QuotaExceededError
from apps.audit import services as audit
from apps.audit.models import AuditEvent
from apps.core.tenancy import tenant_context
from apps.maintenance.models import ItemType, PlanVersion

from .models import (
    FailureCode,
    FailureEvent,
    LaborEntry,
    LaborRate,
    OtherCost,
    PartUsage,
    WorkOrder,
    WorkOrderChecklistItem,
    WorkOrderSequence,
    WorkOrderTransition,
    WorkRequest,
)

S = WorkOrder.Status
R = WorkRequest.Status

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


class WorkOrderError(ValidationError):
    pass


def next_number(*, key: str) -> int:
    """Siguiente correlativo, con bloqueo de fila (sin huecos por concurrencia)."""
    sequence, _created = WorkOrderSequence.objects.select_for_update().get_or_create(key=key)
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

    Idempotente por `client_uuid` (reintentos desde móvil no duplican). Si la
    foto no cabe en el cupo de almacenamiento, el aviso se crea igual sin ella
    y `request.photo_warning` explica por qué: una falla no se pierde por falta
    de espacio.
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
    request.photo_warning = ""
    if photo is not None:
        try:
            add_attachment(work_order=work_order, uploaded_file=photo, by=user)
        except WorkOrderError as error:
            if not isinstance(error.__cause__, QuotaExceededError):
                raise
            request.photo_warning = _("El aviso se envió sin la foto. %(reason)s") % {"reason": error.message}
    return request


def add_attachment(
    *, work_order: WorkOrder, uploaded_file, by, purpose=AttachmentPurpose.PHOTO
) -> Attachment:
    try:
        return attachments_services.add_attachment(
            target_type=AttachmentTargetType.WORK_ORDER,
            target_id=work_order.pk,
            uploaded_file=uploaded_file,
            by=by,
            purpose=purpose,
        )
    except AttachmentError as error:
        raise WorkOrderError(error.message) from error


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
        raise WorkOrderError(
            _("Transición no permitida: %(from)s → %(to)s.")
            % {"from": work_order.get_status_display(), "to": to_status}
        )
    if to_status == S.COMPLETED:
        pending = work_order.checklist.filter(required=True, result="").count()
        if pending:
            raise WorkOrderError(
                _("Faltan %(pending)s ítems obligatorios del checklist.") % {"pending": pending}
            )

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
    recalculate_costs(work_order=work_order)  # los totales quedan fijos al cerrar (WO-04 CA3)
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
        raise WorkOrderError(_("La OT ya no admite cambios."))
    if work_order.status != S.IN_PROGRESS:
        transition(work_order=work_order, to_status=S.IN_PROGRESS, membership=membership)

    if item.item_type == "measurement":
        if value_number in (None, ""):
            raise WorkOrderError(_("Ingresa el valor medido."))
        value = Decimal(str(value_number))
        out_of_range = (item.min_value is not None and value < item.min_value) or (
            item.max_value is not None and value > item.max_value
        )
        item.value_number = value
        result = item.Result.NOT_OK if out_of_range else item.Result.OK
    elif result not in item.Result.values:
        raise WorkOrderError(_("Resultado inválido."))

    item.result = result
    item.note = note[:500]
    item.answered_by = membership.user
    item.answered_at = timezone.now()
    item.save()
    return item


@transaction.atomic
def save_checklist_note(*, item: WorkOrderChecklistItem, membership, note: str) -> WorkOrderChecklistItem:
    """Guarda solo la observación del ítem (WO-03 CA2), sin tocar su resultado.

    Permite escribirla antes o después de marcar el resultado (guardado por ítem).
    """
    require_perm(membership, Perm.WORK_ORDER_EXECUTE)
    if item.work_order.status in (S.COMPLETED, *WorkOrder.CLOSED_STATUSES):
        raise WorkOrderError(_("La OT ya no admite cambios."))
    item.note = note.strip()[:500]
    item.save(update_fields=["note", "updated_at"])
    return item


# --- Checklist de OT no preventivas (WO-03) ------------------------------------

CHECKLIST_LOCKED_STATUSES = (S.COMPLETED, *WorkOrder.CLOSED_STATUSES)


def checklist_editable(work_order: WorkOrder) -> bool:
    """Se agregan ítems a OT no preventivas mientras no estén completadas ni cerradas.

    Las preventivas traen el checklist de su plan (snapshot, ADR-016) y no se tocan.
    """
    return (
        work_order.work_type != WorkOrder.Type.PREVENTIVE
        and work_order.status not in CHECKLIST_LOCKED_STATUSES
    )


def _require_checklist_editable(work_order: WorkOrder) -> None:
    if work_order.work_type == WorkOrder.Type.PREVENTIVE:
        raise WorkOrderError(_("El checklist de una OT preventiva viene de su plan y no se cambia."))
    if work_order.status in CHECKLIST_LOCKED_STATUSES:
        raise WorkOrderError(_("La OT ya no admite cambios."))


def _next_checklist_order(work_order: WorkOrder) -> int:
    last = work_order.checklist.order_by("-order").values_list("order", flat=True).first()
    return (last or 0) + 1


@transaction.atomic
def apply_plan_checklist(
    *, work_order: WorkOrder, plan_version: PlanVersion, membership
) -> list[WorkOrderChecklistItem]:
    """Copia el checklist de una versión publicada a la OT, igual que al generar un preventivo.

    Los ítems se agregan después de los que ya tenga la OT. Aplicar de nuevo
    una versión cuyos ítems ya están (doble envío) se rechaza.
    """
    require_perm(membership, Perm.WORK_ORDER_MANAGE)
    _require_checklist_editable(work_order)
    if (
        plan_version.organization_id != work_order.organization_id
        or plan_version.status != PlanVersion.Status.PUBLISHED
    ):
        raise WorkOrderError(_("Elige un plan con una versión publicada."))
    plan_items = list(plan_version.items.all())
    if not plan_items:
        raise WorkOrderError(_("Ese plan no tiene ítems de checklist."))
    existing = set(work_order.checklist.values_list("text", flat=True))
    if all(item.text in existing for item in plan_items):
        raise WorkOrderError(_("Ese checklist ya está aplicado en la OT."))

    start = _next_checklist_order(work_order)
    # Snapshot, igual que create_work_order: cambiar el plan después no altera esta OT.
    items = WorkOrderChecklistItem.objects.bulk_create(
        [
            WorkOrderChecklistItem(
                organization=work_order.organization,
                work_order=work_order,
                order=start + index,
                text=item.text,
                item_type=item.item_type,
                unit=item.unit,
                min_value=item.min_value,
                max_value=item.max_value,
                required=item.required,
            )
            for index, item in enumerate(plan_items)
        ]
    )
    audit.record(
        action=AuditEvent.Action.UPDATE,
        target=work_order,
        actor=membership.user,
        changes={"checklist_applied": [None, f"{plan_version.plan.code} v{plan_version.number}"]},
    )
    return items


@transaction.atomic
def add_checklist_item(
    *,
    work_order: WorkOrder,
    membership,
    text: str,
    item_type: str = ItemType.CHECK,
    unit: str = "",
    min_value=None,
    max_value=None,
    required: bool = True,
) -> WorkOrderChecklistItem:
    """Agrega un ítem propio (fuera de un plan) al checklist de la OT."""
    require_perm(membership, Perm.WORK_ORDER_MANAGE)
    _require_checklist_editable(work_order)
    text = text.strip()[:250]
    if not text:
        raise WorkOrderError(_("Escribe la tarea del ítem."))
    if item_type not in ItemType.values:
        raise WorkOrderError(_("Tipo de ítem inválido."))
    if item_type == ItemType.CHECK:
        unit, min_value, max_value = "", None, None
    min_value = None if min_value in (None, "") else Decimal(str(min_value))
    max_value = None if max_value in (None, "") else Decimal(str(max_value))
    if min_value is not None and max_value is not None and min_value > max_value:
        raise WorkOrderError(_("El mínimo no puede ser mayor que el máximo."))
    if work_order.checklist.filter(text=text).exists():
        raise WorkOrderError(_("La OT ya tiene el ítem «%(text)s».") % {"text": text})

    item = WorkOrderChecklistItem.objects.create(
        work_order=work_order,
        order=_next_checklist_order(work_order),
        text=text,
        item_type=item_type,
        unit=unit.strip()[:20],
        min_value=min_value,
        max_value=max_value,
        required=required,
    )
    audit.record(
        action=AuditEvent.Action.UPDATE,
        target=work_order,
        actor=membership.user,
        changes={"checklist_item_added": [None, text]},
    )
    return item


def add_checklist_photo(
    *, item: WorkOrderChecklistItem, membership, uploaded_file, client_uuid=None
) -> Attachment:
    """Foto de un ítem del checklist (WO-03 CA2), comprimida y dentro del cupo (AST-06)."""
    require_perm(membership, Perm.WORK_ORDER_EXECUTE)
    if item.work_order.status in CHECKLIST_LOCKED_STATUSES:
        raise WorkOrderError(_("La OT ya no admite cambios."))
    if not (uploaded_file.content_type or "").startswith("image/"):
        raise WorkOrderError(_("Adjunta una imagen (de la cámara o de la galería)."))
    try:
        return attachments_services.add_attachment(
            target_type=AttachmentTargetType.CHECKLIST_ITEM,
            target_id=item.pk,
            uploaded_file=uploaded_file,
            by=membership.user,
            purpose=AttachmentPurpose.PHOTO,
            client_uuid=client_uuid,
        )
    except AttachmentError as error:
        raise WorkOrderError(error.message) from error


def work_order_meter(work_order: WorkOrder):
    """Medidor cuya lectura se pide al cerrar: el del plan o, si no, el primero del activo."""
    if work_order.plan_assignment_id and work_order.plan_assignment.meter_id:
        return work_order.plan_assignment.meter
    return work_order.asset.meters.filter(is_active=True).order_by("kind").first()


def failure_started_default(work_order: WorkOrder):
    """Inicio de la falla si nadie lo informó: el aviso (si lo hubo) o la creación de la OT."""
    request = WorkRequest.objects.filter(work_order=work_order).only("created_at").first()
    return request.created_at if request else work_order.created_at


def _record_failure_at_completion(
    *,
    work_order: WorkOrder,
    user,
    at,
    impact=None,
    started_at=None,
    ended_at=None,
    symptom=None,
    details: dict,
) -> FailureEvent:
    """Registra (o completa) el evento de falla de una OT correctiva (REQ-03 CA1–CA3).

    Si el aviso no marcó "equipo detenido" no hay evento todavía: se crea aquí,
    ya terminado (`ended_at`), y sin tocar el estado del activo — la detención,
    si la hubo, ya pasó.
    """
    failure = FailureEvent.objects.filter(work_order=work_order).first()
    created = failure is None
    if created:
        failure = FailureEvent(
            asset=work_order.asset,
            work_order=work_order,
            impact=FailureEvent.Impact.NONE,
            description=work_order.description,
            started_at=failure_started_default(work_order),
        )
    if impact:
        failure.impact = impact
    if started_at is not None:
        failure.started_at = started_at
    failure.ended_at = ended_at or failure.ended_at or at
    if failure.ended_at < failure.started_at:
        raise WorkOrderError(_("El fin de la detención no puede ser anterior a su inicio."))
    if symptom is not None:
        failure.description = symptom.strip()
    if created or any(details.values()):
        for field, value in details.items():
            setattr(failure, field, value)
    failure.save()
    if created:
        audit.record(
            action=AuditEvent.Action.CREATE, target=failure, actor=user, changes=audit.snapshot(failure)
        )
    return failure


@transaction.atomic
def complete_work_order(
    *,
    work_order: WorkOrder,
    membership,
    labor_minutes: int,
    notes: str = "",
    failure_mode=None,
    failure_cause=None,
    failure_mechanism=None,
    detection_method=None,
    component=None,
    impact: str | None = None,
    failure_started_at=None,
    failure_ended_at=None,
    symptom: str | None = None,
    meter_value=None,
    signature=None,
) -> WorkOrder:
    """Completa la OT; si se informa la lectura del medidor, la registra y reprograma desde ella.

    Toda OT correctiva completada deja su evento de falla (REQ-03 CA2), con
    síntoma observado (`symptom` → `failure_event.description`) y acción
    realizada (`notes` → `completion_notes`) para el asistente (CA3, ADR-018).
    Modo y causa son obligatorios si la organización lo exige
    (`require_failure_details`). `failure_ended_at` por defecto es ahora.
    `signature` (opcional, WO-03 CA3) es la imagen de la firma: queda como
    adjunto de la OT con propósito "firma".
    """
    is_corrective = work_order.work_type == WorkOrder.Type.CORRECTIVE
    if (
        is_corrective
        and work_order.organization.require_failure_details
        and not (failure_mode and failure_cause)
    ):
        raise WorkOrderError(
            _("Esta organización exige modo y causa de falla para cerrar una OT correctiva.")
        )
    if component is not None and component.parent_id != work_order.asset_id:
        raise WorkOrderError(_("El componente debe ser un activo hijo del activo de la OT."))
    if impact and impact not in FailureEvent.Impact.values:
        raise WorkOrderError(_("Impacto de falla inválido."))

    if meter_value is not None:
        meter = work_order_meter(work_order)
        if meter is not None:
            reading, _warnings = record_meter_reading(
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
        log_labor(work_order=work_order, membership=membership, minutes=labor_minutes)
    if signature is not None:
        add_attachment(
            work_order=work_order,
            uploaded_file=signature,
            by=membership.user,
            purpose=AttachmentPurpose.SIGNATURE,
        )
    work_order.completion_notes = notes
    work_order.save(update_fields=["completion_notes", "updated_at"])
    if is_corrective:
        _record_failure_at_completion(
            work_order=work_order,
            user=membership.user,
            at=timezone.now(),
            impact=impact,
            started_at=failure_started_at,
            ended_at=failure_ended_at,
            symptom=symptom,
            details={
                "failure_mode": failure_mode,
                "failure_cause": failure_cause,
                "failure_mechanism": failure_mechanism,
                "detection_method": detection_method,
                "component": component,
            },
        )
    if work_order.status != S.IN_PROGRESS:
        transition(work_order=work_order, to_status=S.IN_PROGRESS, membership=membership)
    return transition(work_order=work_order, to_status=S.COMPLETED, membership=membership)


# --- Costos (WO-04) -----------------------------------------------------------

COST_LOCKED_STATUSES = (S.CLOSED, S.CANCELLED)


def _require_cost_editable(work_order: WorkOrder) -> None:
    if work_order.status in COST_LOCKED_STATUSES:
        raise WorkOrderError(_("La OT está cerrada: sus costos ya no se pueden cambiar."))


def hourly_cost_for(membership) -> Decimal:
    """Tarifa de la persona si tiene una; si no, la general de la organización; si no, 0."""
    rates = dict(
        LaborRate.objects.filter(Q(membership=membership) | Q(membership__isnull=True)).values_list(
            "membership_id", "hourly_cost"
        )
    )
    return rates.get(membership.pk, rates.get(None, Decimal(0)))


def recalculate_costs(*, work_order: WorkOrder) -> WorkOrder:
    """Suma mano de obra, repuestos y otros costos y los guarda en la OT."""
    labor = sum((e.cost for e in work_order.labor.all()), Decimal(0))
    parts = sum((p.cost for p in work_order.parts.all()), Decimal(0))
    other = sum((c.cost for c in work_order.other_costs.all()), Decimal(0))
    work_order.labor_cost, work_order.parts_cost, work_order.other_cost = labor, parts, other
    work_order.total_cost = labor + parts + other
    work_order.save(update_fields=["labor_cost", "parts_cost", "other_cost", "total_cost", "updated_at"])
    return work_order


@transaction.atomic
def log_labor(*, work_order: WorkOrder, membership, minutes: int) -> LaborEntry:
    """Registra horas de la persona con su tarifa vigente copiada (WO-04 CA1)."""
    require_perm(membership, Perm.WORK_ORDER_EXECUTE)
    _require_cost_editable(work_order)
    if minutes <= 0:
        raise WorkOrderError(_("Indica los minutos trabajados."))
    entry = LaborEntry.objects.create(
        work_order=work_order,
        user=membership.user,
        minutes=minutes,
        hourly_cost=hourly_cost_for(membership),
    )
    recalculate_costs(work_order=work_order)
    return entry


@transaction.atomic
def add_part_usage(
    *, work_order: WorkOrder, membership, description: str, quantity, unit_cost, unit: str = "",
    client_uuid=None,
) -> PartUsage:  # fmt: skip
    """Repuesto o material consumido. Idempotente por client_uuid (móvil)."""
    require_perm(membership, Perm.WORK_ORDER_EXECUTE)
    _require_cost_editable(work_order)
    if client_uuid:
        existing = PartUsage.objects.filter(client_uuid=client_uuid).first()
        if existing:
            return existing
    quantity, unit_cost = Decimal(str(quantity)), Decimal(str(unit_cost))
    if quantity <= 0 or unit_cost < 0:
        raise WorkOrderError(_("La cantidad debe ser mayor que cero y el costo no puede ser negativo."))
    part = PartUsage.objects.create(
        work_order=work_order,
        description=description.strip()[:200],
        quantity=quantity,
        unit=unit.strip()[:20],
        unit_cost=unit_cost,
        created_by=membership.user,
        client_uuid=client_uuid,
    )
    recalculate_costs(work_order=work_order)
    return part


@transaction.atomic
def add_other_cost(
    *, work_order: WorkOrder, membership, description: str, amount, supplier: str = ""
) -> OtherCost:
    """Servicio externo u otro costo: lo registra quien gestiona la OT."""
    require_perm(membership, Perm.WORK_ORDER_MANAGE)
    _require_cost_editable(work_order)
    amount = Decimal(str(amount))
    if amount < 0:
        raise WorkOrderError(_("El monto no puede ser negativo."))
    cost = OtherCost.objects.create(
        work_order=work_order,
        description=description.strip()[:200],
        supplier=supplier.strip()[:150],
        amount=amount,
        created_by=membership.user,
    )
    recalculate_costs(work_order=work_order)
    return cost


@transaction.atomic
def remove_cost_line(*, line, membership) -> None:
    """Quita un repuesto (ejecutor) u otro costo (gestor) mientras la OT no esté cerrada."""
    perm = Perm.WORK_ORDER_MANAGE if isinstance(line, OtherCost) else Perm.WORK_ORDER_EXECUTE
    require_perm(membership, perm)
    work_order = line.work_order
    _require_cost_editable(work_order)
    line.delete()
    recalculate_costs(work_order=work_order)


# --- Decisión del planificador sobre solicitudes (REQ-01 CA3) -----------------


def _require_pending_request(work_request: WorkRequest) -> None:
    if work_request.status != R.PENDING:
        raise WorkOrderError(_("La solicitud #%(number)s ya fue decidida.") % {"number": work_request.number})


def _cancel_request_work_order(*, work_request: WorkRequest, membership, comment: str) -> None:
    """Cancela la OT de la solicitud (rechazo o fusión); no se puede si ya se completó el trabajo.

    El evento de falla y el estado del activo (si `asset_stopped` los abrió) no
    se tocan: reflejan la condición física real del equipo, no la decisión
    administrativa sobre el aviso. Quedan en la OT cancelada como historial de
    cuándo se avisó la falla; si el activo sigue detenido, alguien debe
    resolverlo por separado (cambio de estado del activo).
    """
    work_order = work_request.work_order
    if work_order is None:
        return
    if work_order.status in (S.COMPLETED, S.CLOSED):
        raise WorkOrderError(
            _("La %(code)s ya fue completada: la solicitud no se puede rechazar ni fusionar.")
            % {"code": work_order.code}
        )
    if work_order.status != S.CANCELLED:
        transition(work_order=work_order, to_status=S.CANCELLED, membership=membership, comment=comment[:500])


@transaction.atomic
def accept_request(*, work_request: WorkRequest, membership) -> WorkRequest:
    """El planificador acepta la solicitud.

    La OT correctiva ya existe (se crea al avisar, `report_failure`): aceptar
    solo formaliza la decisión y saca la solicitud de la bandeja de pendientes.
    """
    require_perm(membership, Perm.WORK_ORDER_MANAGE)
    _require_pending_request(work_request)
    work_request.status = R.ACCEPTED
    work_request.decided_by = membership.user
    work_request.decided_at = timezone.now()
    work_request.save(update_fields=["status", "decided_by", "decided_at", "updated_at"])
    audit.record(
        action=AuditEvent.Action.TRANSITION,
        target=work_request,
        actor=membership.user,
        changes={"status": [R.PENDING, R.ACCEPTED]},
    )
    return work_request


@transaction.atomic
def reject_request(*, work_request: WorkRequest, membership, reason: str) -> WorkRequest:
    """El planificador rechaza la solicitud (motivo obligatorio) y cancela su OT."""
    require_perm(membership, Perm.WORK_ORDER_MANAGE)
    _require_pending_request(work_request)
    reason = reason.strip()
    if not reason:
        raise WorkOrderError(_("El motivo de rechazo es obligatorio."))

    _cancel_request_work_order(
        work_request=work_request,
        membership=membership,
        comment=_("Solicitud rechazada: %(reason)s") % {"reason": reason},
    )
    work_request.status = R.REJECTED
    work_request.rejection_reason = reason[:500]
    work_request.decided_by = membership.user
    work_request.decided_at = timezone.now()
    work_request.save(update_fields=["status", "rejection_reason", "decided_by", "decided_at", "updated_at"])
    audit.record(
        action=AuditEvent.Action.TRANSITION,
        target=work_request,
        actor=membership.user,
        changes={"status": [R.PENDING, R.REJECTED], "reason": work_request.rejection_reason},
    )
    return work_request


@transaction.atomic
def merge_request(*, work_request: WorkRequest, into: WorkRequest, membership) -> WorkRequest:
    """El planificador fusiona `work_request` con `into`: son el mismo problema.

    Cancela la OT de `work_request` (sin borrado físico: la solicitud queda
    vinculada mediante `merged_into`) y no toca la de `into`.
    """
    require_perm(membership, Perm.WORK_ORDER_MANAGE)
    _require_pending_request(work_request)
    if into.pk == work_request.pk:
        raise WorkOrderError(_("Una solicitud no se puede fusionar consigo misma."))
    if into.asset_id != work_request.asset_id:
        raise WorkOrderError(_("Solo se puede fusionar con una solicitud del mismo activo."))
    if into.status not in (R.PENDING, R.ACCEPTED):
        raise WorkOrderError(_("La solicitud #%(number)s no está abierta.") % {"number": into.number})

    _cancel_request_work_order(
        work_request=work_request,
        membership=membership,
        comment=_("Fusionada con la solicitud #%(number)s") % {"number": into.number},
    )
    work_request.status = R.MERGED
    work_request.merged_into = into
    work_request.decided_by = membership.user
    work_request.decided_at = timezone.now()
    work_request.save(update_fields=["status", "merged_into", "decided_by", "decided_at", "updated_at"])
    audit.record(
        action=AuditEvent.Action.TRANSITION,
        target=work_request,
        actor=membership.user,
        changes={"status": [R.PENDING, R.MERGED], "merged_into": into.number},
    )
    return work_request


# --- Catálogo de falla (REQ-03 CA1) --------------------------------------------

# Catálogo base según la estructura de ISO 14224; cada organización lo recibe al
# crearse y puede editarlo o desactivar códigos que no use (ver `seed_default_failure_catalog`).
DEFAULT_FAILURE_CATALOG: list[tuple[str, str, str]] = [
    (FailureCode.Kind.MODE, "BRD", "Falla total / no funciona"),
    (FailureCode.Kind.MODE, "ELP", "Fuga externa de fluido"),
    (FailureCode.Kind.MODE, "VIB", "Vibración anormal"),
    (FailureCode.Kind.MODE, "OHE", "Sobrecalentamiento"),
    (FailureCode.Kind.MODE, "ERO", "Operación errática"),
    (FailureCode.Kind.MODE, "NOI", "Ruido anormal"),
    (FailureCode.Kind.CAUSE, "DES", "Desgaste"),
    (FailureCode.Kind.CAUSE, "LUB", "Falta de lubricación"),
    (FailureCode.Kind.CAUSE, "ELE", "Falla eléctrica / control"),
    (FailureCode.Kind.CAUSE, "OPE", "Error de operación"),
    (FailureCode.Kind.CAUSE, "MAT", "Falla de material / componente"),
    (FailureCode.Kind.CAUSE, "SUC", "Suciedad / contaminación"),
    (FailureCode.Kind.MECHANISM, "FAT", "Fatiga"),
    (FailureCode.Kind.MECHANISM, "COR", "Corrosión"),
    (FailureCode.Kind.MECHANISM, "WEA", "Desgaste abrasivo"),
    (FailureCode.Kind.MECHANISM, "OVL", "Sobrecarga"),
    (FailureCode.Kind.MECHANISM, "CTM", "Contaminación"),
    (FailureCode.Kind.DETECTION_METHOD, "INS", "Inspección visual"),
    (FailureCode.Kind.DETECTION_METHOD, "OPR", "Aviso del operador"),
    (FailureCode.Kind.DETECTION_METHOD, "MON", "Monitoreo de condición"),
    (FailureCode.Kind.DETECTION_METHOD, "PMT", "Mantenimiento preventivo"),
]


def seed_default_failure_catalog(*, organization) -> list[FailureCode]:
    """Carga el catálogo base de falla (ISO 14224) en la organización; idempotente."""
    with tenant_context(organization):
        existing = set(FailureCode.objects.values_list("kind", "code"))
        codes = [
            FailureCode(organization=organization, kind=kind, code=code, name=name)
            for kind, code, name in DEFAULT_FAILURE_CATALOG
            if (kind, code) not in existing
        ]
        return FailureCode.objects.bulk_create(codes)


@transaction.atomic
def create_failure_code(*, kind: str, code: str, name: str, membership) -> FailureCode:
    require_perm(membership, Perm.WORK_ORDER_MANAGE)
    code, name = code.strip().upper()[:10], name.strip()[:120]
    if not code or not name:
        raise WorkOrderError(_("Código y nombre son obligatorios."))
    if FailureCode.objects.filter(kind=kind, code=code).exists():
        raise WorkOrderError(_("Ya existe un código «%(code)s» de ese tipo.") % {"code": code})
    failure_code = FailureCode.objects.create(kind=kind, code=code, name=name)
    audit.record(
        action=AuditEvent.Action.CREATE,
        target=failure_code,
        actor=membership.user,
        changes=audit.snapshot(failure_code),
    )
    return failure_code


@transaction.atomic
def rename_failure_code(*, failure_code: FailureCode, name: str, membership) -> FailureCode:
    require_perm(membership, Perm.WORK_ORDER_MANAGE)
    name = name.strip()[:120]
    if not name:
        raise WorkOrderError(_("El nombre es obligatorio."))
    old_name = failure_code.name
    if old_name == name:
        return failure_code
    failure_code.name = name
    failure_code.save(update_fields=["name", "updated_at"])
    audit.record(
        action=AuditEvent.Action.UPDATE,
        target=failure_code,
        actor=membership.user,
        changes={"name": [old_name, name]},
    )
    return failure_code


@transaction.atomic
def set_failure_code_active(*, failure_code: FailureCode, is_active: bool, membership) -> FailureCode:
    require_perm(membership, Perm.WORK_ORDER_MANAGE)
    if failure_code.is_active == is_active:
        return failure_code
    failure_code.is_active = is_active
    failure_code.save(update_fields=["is_active", "updated_at"])
    audit.record(
        action=AuditEvent.Action.UPDATE,
        target=failure_code,
        actor=membership.user,
        changes={"is_active": [not is_active, is_active]},
    )
    return failure_code
