"""Reglas de negocio de OT, avisos de falla y checklist."""

import datetime
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.accounts.permissions import Perm, has_perm, require_perm
from apps.assets.models import Asset, MeterReading
from apps.assets.services import change_asset_status, record_meter_reading
from apps.attachments import services as attachments_services
from apps.attachments.models import Attachment
from apps.attachments.models import Purpose as AttachmentPurpose
from apps.attachments.models import TargetType as AttachmentTargetType
from apps.attachments.services import AttachmentError
from apps.audit import services as audit
from apps.audit.models import AuditEvent
from apps.core.tenancy import tenant_context

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
    failure_mechanism=None,
    detection_method=None,
    component=None,
    meter_value=None,
) -> WorkOrder:
    """Completa la OT; si se informa la lectura del medidor, la registra y reprograma desde ella.

    Al completar una OT correctiva se pide completar la falla (REQ-03 CA2): modo
    y causa son obligatorios si la organización lo exige (`require_failure_details`).
    """
    failure = FailureEvent.objects.filter(work_order=work_order).first()
    if (
        failure
        and work_order.work_type == WorkOrder.Type.CORRECTIVE
        and work_order.organization.require_failure_details
        and not (failure_mode and failure_cause)
    ):
        raise WorkOrderError("Esta organización exige modo y causa de falla para cerrar una OT correctiva.")
    if component is not None and component.parent_id != work_order.asset_id:
        raise WorkOrderError("El componente debe ser un activo hijo del activo de la OT.")

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
        log_labor(work_order=work_order, membership=membership, minutes=labor_minutes)
    work_order.completion_notes = notes
    work_order.save(update_fields=["completion_notes", "updated_at"])
    if failure and (failure_mode or failure_cause or failure_mechanism or detection_method or component):
        failure.failure_mode = failure_mode
        failure.failure_cause = failure_cause
        failure.failure_mechanism = failure_mechanism
        failure.detection_method = detection_method
        failure.component = component
        failure.save(
            update_fields=[
                "failure_mode",
                "failure_cause",
                "failure_mechanism",
                "detection_method",
                "component",
                "updated_at",
            ]
        )
    if work_order.status != S.IN_PROGRESS:
        transition(work_order=work_order, to_status=S.IN_PROGRESS, membership=membership)
    return transition(work_order=work_order, to_status=S.COMPLETED, membership=membership)


# --- Costos (WO-04) -----------------------------------------------------------

COST_LOCKED_STATUSES = (S.CLOSED, S.CANCELLED)


def _require_cost_editable(work_order: WorkOrder) -> None:
    if work_order.status in COST_LOCKED_STATUSES:
        raise WorkOrderError("La OT está cerrada: sus costos ya no se pueden cambiar.")


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
        raise WorkOrderError("Indica los minutos trabajados.")
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
        raise WorkOrderError("La cantidad debe ser mayor que cero y el costo no puede ser negativo.")
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
        raise WorkOrderError("El monto no puede ser negativo.")
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
        raise WorkOrderError(f"La solicitud #{work_request.number} ya fue decidida.")


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
            f"La {work_order.code} ya fue completada: la solicitud no se puede rechazar ni fusionar."
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
        raise WorkOrderError("El motivo de rechazo es obligatorio.")

    _cancel_request_work_order(
        work_request=work_request, membership=membership, comment=f"Solicitud rechazada: {reason}"
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
        raise WorkOrderError("Una solicitud no se puede fusionar consigo misma.")
    if into.asset_id != work_request.asset_id:
        raise WorkOrderError("Solo se puede fusionar con una solicitud del mismo activo.")
    if into.status not in (R.PENDING, R.ACCEPTED):
        raise WorkOrderError(f"La solicitud #{into.number} no está abierta.")

    _cancel_request_work_order(
        work_request=work_request,
        membership=membership,
        comment=f"Fusionada con la solicitud #{into.number}",
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
        raise WorkOrderError("Código y nombre son obligatorios.")
    if FailureCode.objects.filter(kind=kind, code=code).exists():
        raise WorkOrderError(f"Ya existe un código «{code}» de ese tipo.")
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
        raise WorkOrderError("El nombre es obligatorio.")
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
