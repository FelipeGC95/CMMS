"""Reglas de negocio de OT, avisos de falla y checklist."""

import datetime
from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy

from apps.accounts.models import Membership
from apps.accounts.permissions import Perm, PermissionDeniedError, has_perm, require_perm
from apps.assets.models import Asset, MeterReading
from apps.assets.services import change_asset_status, record_meter_reading
from apps.attachments import services as attachments_services
from apps.attachments.models import Attachment
from apps.attachments.models import Purpose as AttachmentPurpose
from apps.attachments.models import TargetType as AttachmentTargetType
from apps.attachments.services import AttachmentError, QuotaExceededError
from apps.audit import services as audit
from apps.audit.models import AuditEvent
from apps.core.tenancy import require_current_organization, tenant_context
from apps.maintenance.models import ItemType, PlanVersion
from apps.organizations.models import Location
from apps.ui.templatetags.ui import work_order_status_label

from .models import (
    FailureCode,
    FailureEvent,
    LaborEntry,
    LaborRate,
    OtherCost,
    PartUsage,
    WorkOrder,
    WorkOrderChecklistItem,
    WorkOrderHelper,
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


def _require_single_target(*, asset, location) -> None:
    """Exactamente un objetivo: un activo o una ubicación (WO-01 CA2, REQ-01 CA1)."""
    if (asset is None) == (location is None):
        raise WorkOrderError(_("Elige un activo o una ubicación (solo uno)."))


def _require_members(users, *, organization) -> None:
    """Responsables: solo personas con membresía activa en la organización de la OT."""
    ids = {user.pk for user in users}
    if not ids:
        return
    members = set(
        Membership.objects.filter(organization=organization, is_active=True, user_id__in=ids).values_list(
            "user_id", flat=True
        )
    )
    if ids - members:
        raise WorkOrderError(_("Solo puedes asignar personas de esta organización."))


def _save_helpers(*, work_order: WorkOrder, helpers) -> list:
    """Reemplaza los responsables adicionales; el principal nunca se repite como apoyo."""
    wanted = {user.pk: user for user in helpers if user.pk != work_order.assigned_to_id}
    WorkOrderHelper.objects.filter(work_order=work_order).exclude(user_id__in=wanted).delete()
    existing = set(WorkOrderHelper.objects.filter(work_order=work_order).values_list("user_id", flat=True))
    WorkOrderHelper.objects.bulk_create(
        [
            WorkOrderHelper(organization=work_order.organization, work_order=work_order, user=user)
            for pk, user in wanted.items()
            if pk not in existing
        ]
    )
    return sorted(str(user) for user in wanted.values())


@transaction.atomic
def create_work_order(
    *,
    work_type: str,
    title: str,
    asset: Asset | None = None,
    location: Location | None = None,
    helpers=(),
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
    checklist_sections=(),
    by=None,
    client_uuid=None,
) -> WorkOrder:
    """Crea la OT sobre un activo o una ubicación, con su snapshot de checklist.

    `checklist_sections`: pares (rótulo, ítems) cuando la OT reúne varios
    checklists (absorción de planes, PM-05 CA3); se numeran de corrido.
    """
    _require_single_target(asset=asset, location=location)
    helpers = list(helpers)
    _require_members(
        [*helpers, *([assigned_to] if assigned_to else [])], organization=require_current_organization()
    )
    work_order = WorkOrder.objects.create(
        number=next_number(key=work_type),
        work_type=work_type,
        status=status,
        priority=priority,
        title=title[:200],
        description=description,
        asset=asset,
        location=location,
        due_on=due_on,
        due_meter_value=due_meter_value,
        estimated_minutes=estimated_minutes,
        assigned_to=assigned_to,
        plan_assignment=plan_assignment,
        plan_version=plan_version,
        client_uuid=client_uuid,
    )
    # Snapshot del checklist: cambiar el plan después no altera esta OT.
    if checklist_sections:
        rows = [(label, item) for label, items in checklist_sections for item in items]
        rows = [(index, label, item) for index, (label, item) in enumerate(rows, start=1)]
    else:
        rows = [(item.order, "", item) for item in checklist]
    WorkOrderChecklistItem.objects.bulk_create(
        [
            WorkOrderChecklistItem(
                organization=work_order.organization,
                work_order=work_order,
                order=order,
                section=section[:150],
                text=item.text,
                item_type=item.item_type,
                unit=item.unit,
                min_value=item.min_value,
                max_value=item.max_value,
                required=item.required,
            )
            for order, section, item in rows
        ]
    )
    if helpers:
        _save_helpers(work_order=work_order, helpers=helpers)
    WorkOrderTransition.objects.create(work_order=work_order, to_status=status, by=by)
    return work_order


@transaction.atomic
def set_assignees(*, work_order: WorkOrder, membership, assigned_to=None, helpers=()) -> WorkOrder:
    """Cambia el responsable principal y los de apoyo (WO-01 CA2) y lo deja en la auditoría."""
    require_perm(membership, Perm.WORK_ORDER_MANAGE)
    if work_order.status in WorkOrder.CLOSED_STATUSES:
        raise WorkOrderError(_("La OT ya no admite cambios."))
    helpers = list(helpers)
    _require_members(
        [*helpers, *([assigned_to] if assigned_to else [])], organization=membership.organization
    )
    before_lead = str(work_order.assigned_to) if work_order.assigned_to_id else ""
    before_helpers = sorted(str(user) for user in work_order.helpers.all())
    work_order.assigned_to = assigned_to
    work_order.save(update_fields=["assigned_to", "updated_at"])
    after_helpers = _save_helpers(work_order=work_order, helpers=helpers)
    after_lead = str(assigned_to) if assigned_to else ""
    changes = {}
    if before_lead != after_lead:
        changes["assigned_to"] = [before_lead, after_lead]
    if before_helpers != after_helpers:
        changes["helpers"] = [before_helpers, after_helpers]
    if changes:
        audit.record(
            action=AuditEvent.Action.UPDATE, target=work_order, actor=membership.user, changes=changes
        )
    return work_order


def _suggest_priority(asset: Asset | None, asset_stopped: bool) -> int:
    if asset is None:
        return WorkOrder.Priority.NORMAL
    if asset_stopped and asset.criticality == Asset.Criticality.A:
        return WorkOrder.Priority.URGENT
    if asset_stopped:
        return WorkOrder.Priority.HIGH
    return WorkOrder.Priority.NORMAL


@transaction.atomic
def report_failure(
    *,
    membership,
    description: str,
    asset: Asset | None = None,
    location: Location | None = None,
    asset_stopped: bool = False,
    photo=None,
    client_uuid=None,
) -> WorkRequest:
    """Aviso de falla (REQ-01): crea la solicitud y su OT correctiva en un paso.

    El aviso apunta a un activo o a una ubicación (CA1); "equipo detenido"
    solo aplica a un activo (una ubicación no tiene estado que detener).

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
    _require_single_target(asset=asset, location=location)
    if asset_stopped and asset is None:
        raise WorkOrderError(_("«Equipo detenido» solo aplica a un activo."))

    user = membership.user
    now = timezone.now()
    work_order = create_work_order(
        work_type=WorkOrder.Type.CORRECTIVE,
        title=description.strip().splitlines()[0][:120] or _("Falla reportada"),
        description=description,
        asset=asset,
        location=location,
        priority=_suggest_priority(asset, asset_stopped),
        status=S.REQUESTED,
        due_on=timezone.localdate() if asset_stopped else None,
        by=user,
    )
    request = WorkRequest.objects.create(
        number=next_number(key="request"),
        asset=asset,
        location=location,
        description=description,
        asset_stopped=asset_stopped,
        requested_by=user,
        work_order=work_order,
        client_uuid=client_uuid,
    )
    if asset_stopped:
        failure = FailureEvent.objects.create(
            asset=asset,
            work_order=work_order,
            impact=FailureEvent.Impact.TOTAL_STOP,
            description=description,
            started_at=now,
        )
        change_asset_status(
            asset=asset,
            status=Asset.Status.STOPPED,
            by=user,
            reason=work_order.code,
            at=now,
            work_order=work_order,
            failure_event=failure,
        )
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
            % {
                "from": work_order_status_label(work_order.status),
                "to": work_order_status_label(to_status),
            }
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
        # Una OT sobre una ubicación no toca el estado de ningún activo.
        if work_order.asset_id and work_order.asset.status == Asset.Status.STOPPED:
            change_asset_status(
                asset=work_order.asset,
                status=Asset.Status.IN_MAINTENANCE,
                by=user,
                reason=work_order.code,
                at=now,
                work_order=work_order,
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
            comment=_("Cierre automático (la organización no exige aprobación)"),
        )
    recalculate_costs(work_order=work_order)  # los totales quedan fijos al cerrar (WO-04 CA3)
    work_order.status = S.CLOSED
    work_order.closed_at = at
    work_order.save(update_fields=["status", "closed_at", "updated_at"])

    # La falla de esta OT (si la hay): se cierra aquí si nadie informó su fin al completar.
    failure = FailureEvent.objects.filter(work_order=work_order).first()
    if failure and failure.ended_at is None:
        failure.ended_at = at
        failure.save(update_fields=["ended_at", "updated_at"])

    asset = work_order.asset
    if asset is not None and asset.status in (Asset.Status.STOPPED, Asset.Status.IN_MAINTENANCE):
        if not FailureEvent.objects.filter(asset=asset, ended_at__isnull=True).exists():
            change_asset_status(
                asset=asset,
                status=Asset.Status.OPERATIONAL,
                by=user,
                reason=work_order.code,
                at=at,
                work_order=work_order,
                failure_event=failure,
            )
    if work_order.plan_assignment_id:
        register_completion(
            assignment=work_order.plan_assignment,
            completed_on=timezone.localdate(at),
            meter_value=work_order.meter_value_at_completion,
        )
        # Planes absorbidos (PM-05 CA3): la misma ejecución los reprograma a todos.
        main_meter_id = work_order.plan_assignment.meter_id
        for absorption in work_order.absorptions.select_related(
            "plan_assignment__plan__current_version", "plan_assignment__meter"
        ):
            assignment = absorption.plan_assignment
            same_meter = assignment.meter_id is not None and assignment.meter_id == main_meter_id
            register_completion(
                assignment=assignment,
                completed_on=timezone.localdate(at),
                meter_value=work_order.meter_value_at_completion if same_meter else None,
            )


MEASUREMENT_LIMIT = Decimal(10) ** 11  # max_digits=14, decimal_places=3


def parse_measurement(raw) -> Decimal:
    """Valor medido como lo escribe un técnico en es-CL: coma o punto decimal, sin miles (WO-03 CA2).

    La pantalla muestra las mediciones con coma decimal y sin separador de
    miles (filtro `measure`), así que «7,5» y «7.5» valen 7,5 y el valor
    guardado vuelve tal cual al campo.
    """
    text = str(raw if raw is not None else "").strip().replace(" ", "").replace(",", ".")
    if not text:
        raise WorkOrderError(_("Ingresa el valor medido."))
    try:
        value = Decimal(text)
    except InvalidOperation as error:
        raise WorkOrderError(_("Ingresa un número, por ejemplo 7,5.")) from error
    if not value.is_finite() or abs(value) >= MEASUREMENT_LIMIT:
        raise WorkOrderError(_("Ingresa un número, por ejemplo 7,5."))
    return value.quantize(Decimal("0.001"))


TEXT_ANSWER_MAX = 1000


def _start_if_needed(*, work_order: WorkOrder, membership) -> None:
    """La primera respuesta (resultado, foto o firma) inicia la OT."""
    if work_order.status in CHECKLIST_LOCKED_STATUSES:
        raise WorkOrderError(_("La OT ya no admite cambios."))
    if work_order.status != S.IN_PROGRESS:
        transition(work_order=work_order, to_status=S.IN_PROGRESS, membership=membership)


@transaction.atomic
def answer_checklist_item(
    *,
    item: WorkOrderChecklistItem,
    membership,
    result: str = "",
    value_number=None,
    value_text: str = "",
    signature=None,
    note="",
) -> WorkOrderChecklistItem:
    """Registra la respuesta de un ítem según su tipo (PM-01 CA1, WO-03 CA2).

    Verificación: OK / No OK / N.A. Medición: el valor, validado contra el
    rango (fuera de rango = No OK). Texto: la respuesta escrita. Firma: la
    imagen dibujada (adjunto del ítem con propósito «firma»). La foto
    obligatoria se responde subiendo una foto (`add_checklist_photo`). La
    primera respuesta inicia la OT.
    """
    require_perm(membership, Perm.WORK_ORDER_EXECUTE)
    work_order = item.work_order
    if item.item_type == ItemType.PHOTO:
        raise WorkOrderError(_("Este ítem se responde con una foto."))
    if item.item_type == ItemType.TEXT:
        value_text = (value_text or "").strip()[:TEXT_ANSWER_MAX]
        if not value_text:
            raise WorkOrderError(_("Escribe la respuesta."))
    if item.item_type == ItemType.SIGNATURE and signature is None:
        raise WorkOrderError(_("Firma dentro del recuadro antes de guardar."))
    _start_if_needed(work_order=work_order, membership=membership)

    if item.item_type == ItemType.MEASUREMENT:
        value = parse_measurement(value_number)
        out_of_range = (item.min_value is not None and value < item.min_value) or (
            item.max_value is not None and value > item.max_value
        )
        item.value_number = value
        result = item.Result.NOT_OK if out_of_range else item.Result.OK
    elif item.item_type == ItemType.TEXT:
        item.value_text = value_text
        result = item.Result.DONE
    elif item.item_type == ItemType.SIGNATURE:
        try:
            attachments_services.add_attachment(
                target_type=AttachmentTargetType.CHECKLIST_ITEM,
                target_id=item.pk,
                uploaded_file=signature,
                by=membership.user,
                purpose=AttachmentPurpose.SIGNATURE,
            )
        except AttachmentError as error:
            raise WorkOrderError(error.message) from error
        result = item.Result.DONE
    elif result not in (item.Result.OK, item.Result.NOT_OK, item.Result.NA):
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
    if item_type != ItemType.MEASUREMENT:
        unit, min_value, max_value = "", None, None
    required = required or item_type == ItemType.PHOTO  # «foto obligatoria» siempre bloquea el cierre
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


def _renumber_checklist(items) -> None:
    """Numera 1..n en el orden recibido; solo escribe los ítems que cambian."""
    changed = []
    now = timezone.now()
    for number, item in enumerate(items, start=1):
        if item.order != number:
            item.order = number
            item.updated_at = now
            changed.append(item)
    WorkOrderChecklistItem.objects.bulk_update(changed, ["order", "updated_at"])


def _locked_checklist(work_order: WorkOrder) -> list[WorkOrderChecklistItem]:
    """Ítems de la OT bloqueados para reordenar o quitar sin carreras entre dos gestores."""
    return list(
        WorkOrderChecklistItem.objects.select_for_update()
        .filter(work_order_id=work_order.pk)
        .order_by("order", "created_at")
    )


def checklist_item_has_evidence(item: WorkOrderChecklistItem, *, photo_count: int | None = None) -> bool:
    """Un ítem con resultado, valor, observación o fotos ya es parte de la ejecución."""
    if item.result or item.value_number is not None or item.value_text or item.note:
        return True
    if photo_count is None:
        photo_count = Attachment.objects.filter(
            target_type=AttachmentTargetType.CHECKLIST_ITEM, target_id=item.pk
        ).count()
    return photo_count > 0


@transaction.atomic
def remove_checklist_item(*, item: WorkOrderChecklistItem, membership) -> None:
    """Quita un ítem sin responder de una OT no preventiva abierta (WO-03).

    Un ítem sin respuesta es solo un borrador del checklist: se borra y queda
    el rastro en la auditoría de la OT. Uno con respuesta, observación o fotos
    es historial de la ejecución y no se quita.
    """
    require_perm(membership, Perm.WORK_ORDER_MANAGE)
    work_order = item.work_order
    _require_checklist_editable(work_order)
    items = _locked_checklist(work_order)
    current = next((i for i in items if i.pk == item.pk), None)
    if current is None:
        raise WorkOrderError(_("Ese ítem ya no está en el checklist."))
    if checklist_item_has_evidence(current):
        raise WorkOrderError(
            _("«%(text)s» ya tiene respuesta, observación o fotos: no se puede quitar.")
            % {"text": current.text}
        )
    remaining = [i for i in items if i.pk != current.pk]
    current.delete()
    _renumber_checklist(remaining)
    audit.record(
        action=AuditEvent.Action.UPDATE,
        target=work_order,
        actor=membership.user,
        changes={"checklist_item_removed": [current.text, None]},
    )


CHECKLIST_MOVES = {"up": -1, "down": 1}


@transaction.atomic
def move_checklist_item(*, item: WorkOrderChecklistItem, membership, direction: str) -> None:
    """Sube o baja un ítem una posición y renumera el checklist (WO-03).

    Mismas reglas que agregar ítems: OT no preventiva (el orden de una
    preventiva viene de su plan) y aún no completada.
    """
    require_perm(membership, Perm.WORK_ORDER_MANAGE)
    if direction not in CHECKLIST_MOVES:
        raise WorkOrderError(_("Movimiento inválido."))
    work_order = item.work_order
    _require_checklist_editable(work_order)
    items = _locked_checklist(work_order)
    index = next((n for n, i in enumerate(items) if i.pk == item.pk), None)
    if index is None:
        raise WorkOrderError(_("Ese ítem ya no está en el checklist."))
    target = index + CHECKLIST_MOVES[direction]
    if not 0 <= target < len(items):
        return  # ya está en el extremo: nada que hacer
    items[index], items[target] = items[target], items[index]
    _renumber_checklist(items)
    audit.record(
        action=AuditEvent.Action.UPDATE,
        target=work_order,
        actor=membership.user,
        changes={"checklist_item_moved": [f"{index + 1}. {item.text}", f"{target + 1}. {item.text}"]},
    )


@transaction.atomic
def add_checklist_photo(
    *, item: WorkOrderChecklistItem, membership, uploaded_file, client_uuid=None
) -> Attachment:
    """Foto de un ítem del checklist (WO-03 CA2), comprimida y dentro del cupo (AST-06).

    En un ítem «foto obligatoria» la foto es la respuesta: lo marca como hecho
    e inicia la OT, igual que responder cualquier otro ítem.
    """
    require_perm(membership, Perm.WORK_ORDER_EXECUTE)
    if item.work_order.status in CHECKLIST_LOCKED_STATUSES:
        raise WorkOrderError(_("La OT ya no admite cambios."))
    if not (uploaded_file.content_type or "").startswith("image/"):
        raise WorkOrderError(_("Adjunta una imagen (de la cámara o de la galería)."))
    try:
        photo = attachments_services.add_attachment(
            target_type=AttachmentTargetType.CHECKLIST_ITEM,
            target_id=item.pk,
            uploaded_file=uploaded_file,
            by=membership.user,
            purpose=AttachmentPurpose.PHOTO,
            client_uuid=client_uuid,
        )
    except AttachmentError as error:
        raise WorkOrderError(error.message) from error
    if item.item_type == ItemType.PHOTO and item.result != item.Result.DONE:
        _start_if_needed(work_order=item.work_order, membership=membership)
        item.result = item.Result.DONE
        item.answered_by = membership.user
        item.answered_at = timezone.now()
        item.save(update_fields=["result", "answered_by", "answered_at", "updated_at"])
    return photo


def can_remove_checklist_photo(*, photo: Attachment, membership) -> bool:
    """Quien la subió (si ejecuta OT) o un gestor."""
    if has_perm(membership, Perm.WORK_ORDER_MANAGE):
        return True
    return has_perm(membership, Perm.WORK_ORDER_EXECUTE) and photo.uploaded_by_id == membership.user_id


@transaction.atomic
def remove_checklist_photo(*, item: WorkOrderChecklistItem, photo: Attachment, membership) -> None:
    """Quita una foto de un ítem mientras la OT no esté completada (WO-03 CA2).

    Las fotos de una OT abierta son borrador de la ejecución (una foto movida,
    la de otro equipo): se borran de verdad, archivo incluido, y liberan el
    cupo. La auditoría conserva quién la quitó y su huella (SHA-256). Una vez
    completada la OT, sus fotos son historial y ya no se quitan.
    """
    if not can_remove_checklist_photo(photo=photo, membership=membership):
        raise PermissionDeniedError(Perm.WORK_ORDER_MANAGE)
    if (photo.target_type, photo.target_id) != (AttachmentTargetType.CHECKLIST_ITEM, item.pk):
        raise WorkOrderError(_("Esa foto no es de este ítem."))
    if item.work_order.status in CHECKLIST_LOCKED_STATUSES:
        raise WorkOrderError(_("La OT ya no admite cambios."))
    attachments_services.remove_attachment(attachment=photo, by=membership.user)
    if item.item_type == ItemType.PHOTO and not _item_photos(item).exists():
        item.result, item.answered_by, item.answered_at = "", None, None  # vuelve a quedar pendiente
        item.save(update_fields=["result", "answered_by", "answered_at", "updated_at"])


def _item_photos(item: WorkOrderChecklistItem):
    return Attachment.objects.filter(
        target_type=AttachmentTargetType.CHECKLIST_ITEM, target_id=item.pk, purpose=AttachmentPurpose.PHOTO
    )


def work_order_meter(work_order: WorkOrder):
    """Medidor cuya lectura se pide al cerrar: el del plan o, si no, el primero del activo."""
    if work_order.plan_assignment_id and work_order.plan_assignment.meter_id:
        return work_order.plan_assignment.meter
    if work_order.asset_id is None:
        return None  # una ubicación no tiene medidores
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
    Una correctiva sobre una ubicación no deja evento de falla: los eventos
    (y MTBF/MTTR) son por activo.
    `signature` (opcional, WO-03 CA3) es la imagen de la firma: queda como
    adjunto de la OT con propósito "firma". Si no cabe en el cupo de
    almacenamiento, la OT se completa igual sin ella y
    `work_order.signature_warning` explica por qué (como la foto del aviso).
    """
    is_corrective = work_order.work_type == WorkOrder.Type.CORRECTIVE and work_order.asset_id is not None
    if (
        is_corrective
        and work_order.organization.require_failure_details
        and not (failure_mode and failure_cause)
    ):
        raise WorkOrderError(
            _("Esta organización exige modo y causa de falla para cerrar una OT correctiva.")
        )
    if component is not None and (work_order.asset_id is None or component.parent_id != work_order.asset_id):
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
    signature_warning = ""
    if signature is not None:
        try:
            add_attachment(
                work_order=work_order,
                uploaded_file=signature,
                by=membership.user,
                purpose=AttachmentPurpose.SIGNATURE,
            )
        except WorkOrderError as error:
            if not isinstance(error.__cause__, QuotaExceededError):
                raise
            signature_warning = _("La OT se completó sin la firma. %(reason)s") % {"reason": error.message}
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
    work_order = transition(work_order=work_order, to_status=S.COMPLETED, membership=membership)
    work_order.signature_warning = signature_warning
    return work_order


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
    if (into.asset_id, into.location_id) != (work_request.asset_id, work_request.location_id):
        raise WorkOrderError(_("Solo se puede fusionar con una solicitud del mismo activo o ubicación."))
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
# Los nombres se guardan en el idioma activo al crear la organización.
DEFAULT_FAILURE_CATALOG: list[tuple[str, str, str]] = [
    (FailureCode.Kind.MODE, "BRD", gettext_lazy("Falla total / no funciona")),
    (FailureCode.Kind.MODE, "ELP", gettext_lazy("Fuga externa de fluido")),
    (FailureCode.Kind.MODE, "VIB", gettext_lazy("Vibración anormal")),
    (FailureCode.Kind.MODE, "OHE", gettext_lazy("Sobrecalentamiento")),
    (FailureCode.Kind.MODE, "ERO", gettext_lazy("Operación errática")),
    (FailureCode.Kind.MODE, "NOI", gettext_lazy("Ruido anormal")),
    (FailureCode.Kind.CAUSE, "DES", gettext_lazy("Desgaste")),
    (FailureCode.Kind.CAUSE, "LUB", gettext_lazy("Falta de lubricación")),
    (FailureCode.Kind.CAUSE, "ELE", gettext_lazy("Falla eléctrica / control")),
    (FailureCode.Kind.CAUSE, "OPE", gettext_lazy("Error de operación")),
    (FailureCode.Kind.CAUSE, "MAT", gettext_lazy("Falla de material / componente")),
    (FailureCode.Kind.CAUSE, "SUC", gettext_lazy("Suciedad / contaminación")),
    (FailureCode.Kind.MECHANISM, "FAT", gettext_lazy("Fatiga")),
    (FailureCode.Kind.MECHANISM, "COR", gettext_lazy("Corrosión")),
    (FailureCode.Kind.MECHANISM, "WEA", gettext_lazy("Desgaste abrasivo")),
    (FailureCode.Kind.MECHANISM, "OVL", gettext_lazy("Sobrecarga")),
    (FailureCode.Kind.MECHANISM, "CTM", gettext_lazy("Contaminación")),
    (FailureCode.Kind.DETECTION_METHOD, "INS", gettext_lazy("Inspección visual")),
    (FailureCode.Kind.DETECTION_METHOD, "OPR", gettext_lazy("Aviso del operador")),
    (FailureCode.Kind.DETECTION_METHOD, "MON", gettext_lazy("Monitoreo de condición")),
    (FailureCode.Kind.DETECTION_METHOD, "PMT", gettext_lazy("Mantenimiento preventivo")),
]


def seed_default_failure_catalog(*, organization) -> list[FailureCode]:
    """Carga el catálogo base de falla (ISO 14224) en la organización; idempotente."""
    with tenant_context(organization):
        existing = set(FailureCode.objects.values_list("kind", "code"))
        codes = [
            FailureCode(organization=organization, kind=kind, code=code, name=str(name))
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
