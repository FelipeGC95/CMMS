"""Tokens de API (X-05): alta, revocación y resolución del principal de servicio."""

import datetime
import hashlib
import secrets

from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext as _
from rest_framework.exceptions import APIException

from apps.accounts.models import Membership, User
from apps.accounts.permissions import Perm, require_perm
from apps.attachments.models import Attachment
from apps.attachments.models import TargetType as AttachmentTargetType
from apps.audit import services as audit
from apps.audit.models import AuditEvent
from apps.audit.services import API_TOKEN_EMAIL_DOMAIN
from apps.condition.models import ConditionRule
from apps.condition.services import create_rule
from apps.inventory import services as inventory
from apps.inventory.models import StockMovement
from apps.inventory.services import InventoryError
from apps.work.models import LaborEntry, PartUsage, WorkOrder, WorkOrderChecklistItem
from apps.work.services import (
    ERROR_CANCELLED,
    ERROR_CONFLICT,
    ERROR_LOCKED,
    WorkOrderError,
    add_checklist_photo,
    add_part_usage,
    add_work_order_photo,
    answer_checklist_item,
    complete_work_order,
    create_corrective_from_item,
    create_work_order,
    log_labor,
    parse_measurement,
    transition,
)

from .models import ApiIdempotencyKey, ApiToken

TOKEN_PREFIX = "gbt_"
# Rol con que actúa un token: escribe como planificador (activos, lecturas, OT y
# solicitudes), nunca administra equipo, auditoría, configuración ni otros tokens.
# El alcance "read" además limita la API a métodos de lectura (framework.HasTokenScope).
SERVICE_ROLE = Membership.Role.PLANNER
# Actualizar "último uso" como mucho una vez por minuto: evita una escritura por petición.
LAST_USED_RESOLUTION = datetime.timedelta(minutes=1)


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


@transaction.atomic
def create_token(
    *, membership, name: str, scope: str, expires_at: datetime.datetime | None = None
) -> tuple[ApiToken, str]:
    """Crea el token y devuelve (token, valor completo). El valor no se vuelve a mostrar."""
    require_perm(membership, Perm.API_TOKEN_MANAGE)
    prefix = secrets.token_hex(4)
    raw = f"{TOKEN_PREFIX}{prefix}_{secrets.token_urlsafe(32)}"
    service_user = User.objects.create_user(
        f"api-{prefix}{API_TOKEN_EMAIL_DOMAIN}",
        None,  # clave inutilizable: no puede iniciar sesión
        full_name=f"API · {name.strip()}"[:150],
        is_active=False,
    )
    token = ApiToken.objects.create(
        name=name.strip(),
        scope=scope,
        prefix=prefix,
        token_hash=hash_token(raw),
        service_user=service_user,
        created_by=membership.user,
        expires_at=expires_at,
    )
    audit.record(
        action=AuditEvent.Action.CREATE,
        target=token,
        membership=membership,
        changes={"name": token.name, "scope": scope, "prefix": prefix, "expires_at": expires_at},
    )
    return token, raw


@transaction.atomic
def revoke_token(*, membership, token: ApiToken) -> ApiToken:
    require_perm(membership, Perm.API_TOKEN_MANAGE)
    if token.revoked_at is None:
        token.revoked_at = timezone.now()
        token.save(update_fields=["revoked_at", "updated_at"])
        audit.record(
            action=AuditEvent.Action.REVOKE,
            target=token,
            membership=membership,
            changes={"revoked_at": [None, token.revoked_at]},
        )
    return token


def authenticate_token(raw: str) -> ApiToken | None:
    """Token vigente de una organización activa, o None. Marca el último uso."""
    if not raw.startswith(TOKEN_PREFIX):
        return None
    # all_tenants: la organización aún no se conoce; se deduce del token (ADR-003).
    token = (
        ApiToken.all_tenants.select_related("organization", "service_user")
        .filter(token_hash=hash_token(raw), organization__is_active=True)
        .first()
    )
    if token is None or not token.is_usable:
        return None
    now = timezone.now()
    if token.last_used_at is None or now - token.last_used_at >= LAST_USED_RESOLUTION:
        # all_tenants: actualización puntual del token recién resuelto, sin contexto aún.
        ApiToken.all_tenants.filter(pk=token.pk).update(last_used_at=now)
        token.last_used_at = now
    return token


def principal_for(token: ApiToken) -> Membership:
    """Membresía en memoria (no se guarda) con que el token llama a los servicios.

    Toda la organización (sin alcance por sitio) y los permisos de SERVICE_ROLE.
    """
    principal = Membership(
        user=token.service_user,
        organization=token.organization,
        role=SERVICE_ROLE,
        restricted_to_locations=False,
    )
    principal.api_token = token
    return principal


def create_work_order_idempotent(*, membership, client_uuid=None, **data) -> WorkOrder:
    """Alta de OT desde la API: exige gestionar OT y no duplica si `client_uuid` ya existe."""
    require_perm(membership, Perm.WORK_ORDER_MANAGE)
    if client_uuid:
        existing = WorkOrder.objects.filter(client_uuid=client_uuid).first()
        if existing:
            return existing
    return create_work_order(by=membership.user, client_uuid=client_uuid, **data)


class Conflict(APIException):
    """La petición choca con el estado actual (un `client_uuid` ya usado en otra operación)."""

    status_code = 409
    default_code = "conflict"


# Códigos de `WorkOrderError` que la API de terreno responde con 409 (X-06): OT cancelada,
# OT completada o cerrada, y `client_uuid` ya usado en otra cosa. La API v1 los sigue
# respondiendo con 400, como siempre.
CONFLICT_CODES = (ERROR_CANCELLED, ERROR_LOCKED, ERROR_CONFLICT)

COMPLETE_ACTION = "work_order.complete"
TRANSITION_ACTION = "work_order.transition"


def _require_not_finished(work_order: WorkOrder) -> None:
    """Sin conexión no se reabre: una OT completada, cerrada o cancelada es un conflicto (409)."""
    if work_order.status == WorkOrder.Status.CANCELLED:
        raise WorkOrderError(_("La OT fue cancelada."), code=ERROR_CANCELLED)
    if work_order.status in (WorkOrder.Status.COMPLETED, WorkOrder.Status.CLOSED):
        raise WorkOrderError(_("La OT ya está completada."), code=ERROR_LOCKED)


def _replayed_key(*, client_uuid, action: str, target_id) -> bool:
    """¿Esta clave ya hizo esta misma acción sobre este objeto? Usada en otra cosa → 409."""
    if not client_uuid:
        return False
    key = ApiIdempotencyKey.objects.filter(client_uuid=client_uuid).first()
    if key is None:
        return False
    if key.action == action and key.target_id == target_id:
        return True
    raise Conflict(_("Este client_uuid ya se usó para otra operación."))


@transaction.atomic
def transition_work_order_idempotent(
    *,
    membership,
    work_order: WorkOrder,
    to_status: str,
    comment: str = "",
    client_uuid=None,
    at=None,
    reopen: bool = True,
) -> WorkOrder:
    """Cambio de estado desde la API (WO-02), idempotente por `client_uuid`.

    Repetir el mismo destino no hace nada. Con `client_uuid`, reenviar el cambio
    (aunque la OT ya haya avanzado) devuelve su estado actual sin repetirlo. `at` es
    la hora real del cambio sin conexión (`transition(at=)`, acotada con `client_time`).
    `reopen=False` (terreno): una OT ya completada, cerrada o cancelada no se toca → 409.
    """
    require_perm(membership, Perm.WORK_ORDER_EXECUTE)
    work_order = WorkOrder.objects.select_for_update().get(pk=work_order.pk)
    if _replayed_key(client_uuid=client_uuid, action=TRANSITION_ACTION, target_id=work_order.pk):
        return work_order
    if not reopen:
        _require_not_finished(work_order)
    if work_order.status != to_status:
        work_order = transition(
            work_order=work_order, to_status=to_status, membership=membership, comment=comment, at=at
        )
    if client_uuid:
        ApiIdempotencyKey.objects.create(
            client_uuid=client_uuid, action=TRANSITION_ACTION, target_id=work_order.pk
        )
    return work_order


CONDITION_RULE_CREATE_ACTION = "condition_rule.create"


@transaction.atomic
def create_condition_rule_idempotent(*, membership, client_uuid=None, **data) -> tuple[ConditionRule, bool]:
    """Alta de una regla de condición desde la API (CBM-09); devuelve (regla, creada).

    El mismo servicio que la web (`condition.services.create_rule`). Reenviar el mismo
    `client_uuid` devuelve la regla ya creada; usarlo para otra operación → 409.
    """
    require_perm(membership, Perm.CONDITION_MANAGE)
    if client_uuid:
        key = ApiIdempotencyKey.objects.filter(client_uuid=client_uuid).first()
        if key is not None:
            rule = ConditionRule.objects.filter(pk=key.target_id).first()
            if key.action != CONDITION_RULE_CREATE_ACTION or rule is None:
                raise Conflict(_("Este client_uuid ya se usó para otra operación."))
            return rule, False
    rule = create_rule(membership=membership, **data)
    if client_uuid:
        ApiIdempotencyKey.objects.create(
            client_uuid=client_uuid, action=CONDITION_RULE_CREATE_ACTION, target_id=rule.pk
        )
    return rule, True


FINDING_ACTION = "checklist_item.finding"


@transaction.atomic
def report_finding_idempotent(
    *, membership, item, description: str = "", asset_stopped: bool = False, client_uuid=None
) -> tuple[WorkOrder, bool]:
    """Hallazgo de un ítem No OK avisado sin conexión (WO-06 CA2); devuelve (correctiva, creada).

    El mismo servicio que la web (`work.services.create_corrective_from_item`): quien gestiona
    OT crea la correctiva; quien no (técnico) pasa por el aviso de falla (solicitud + correctiva).
    Idempotente por ítem (con una correctiva abierta, la devuelve) y por `client_uuid`: reenviarlo
    devuelve la correctiva ya creada aunque la OT de origen haya avanzado; usarlo en otra
    operación u otro ítem → 409. Sin conexión no se avisa sobre una OT terminada: cancelada →
    409 `cancelled`, completada o cerrada → 409 `locked`. Ítem que no está No OK → 400.
    """
    require_perm(membership, Perm.REQUEST_CREATE)
    if client_uuid:
        key = ApiIdempotencyKey.objects.filter(client_uuid=client_uuid).first()
        if key is not None:
            corrective = WorkOrder.objects.filter(pk=key.target_id, source_checklist_item=item).first()
            if key.action != FINDING_ACTION or corrective is None:
                raise Conflict(_("Este client_uuid ya se usó para otra operación."))
            return corrective, False
    # La OT de origen se bloquea: dos avisos simultáneos del mismo ítem crean una sola correctiva.
    parent = WorkOrder.objects.select_for_update().get(pk=item.work_order_id)
    _require_not_finished(parent)
    item = WorkOrderChecklistItem.objects.select_related("work_order").get(pk=item.pk)
    already_open = (
        WorkOrder.objects.filter(source_checklist_item=item)
        .exclude(status__in=WorkOrder.CLOSED_STATUSES)
        .exists()
    )
    corrective = create_corrective_from_item(
        item=item, membership=membership, description=description, asset_stopped=asset_stopped
    )
    if client_uuid:
        ApiIdempotencyKey.objects.create(
            client_uuid=client_uuid, action=FINDING_ACTION, target_id=corrective.pk
        )
    return corrective, not already_open


def log_labor_idempotent(
    *, membership, work_order: WorkOrder, minutes: int, client_uuid=None
) -> tuple[LaborEntry, bool]:
    """Horas de la persona en la OT (WO-04); devuelve (registro, creado).

    Reenviar el mismo `client_uuid` devuelve el registro ya creado (`log_labor`).
    """
    replay = bool(client_uuid) and LaborEntry.objects.filter(client_uuid=client_uuid).exists()
    entry = log_labor(work_order=work_order, membership=membership, minutes=minutes, client_uuid=client_uuid)
    return entry, not replay


@transaction.atomic
def complete_work_order_idempotent(
    *, membership, work_order: WorkOrder, client_uuid=None, reopen: bool = True, **data
) -> WorkOrder:
    """Cierre de OT desde la API (WO-03): el mismo servicio que la web, idempotente por `client_uuid`.

    La OT se bloquea mientras se decide: dos envíos simultáneos con la misma clave
    no la completan dos veces. Si la clave ya completó esta OT, devuelve su
    estado actual sin repetir nada. `reopen=False` (terreno): una OT ya completada por
    otra persona no se reabre para completarla de nuevo → 409.
    """
    require_perm(membership, Perm.WORK_ORDER_EXECUTE)
    work_order = WorkOrder.objects.select_for_update().get(pk=work_order.pk)
    if _replayed_key(client_uuid=client_uuid, action=COMPLETE_ACTION, target_id=work_order.pk):
        return work_order
    if not reopen:
        _require_not_finished(work_order)
    work_order = complete_work_order(work_order=work_order, membership=membership, **data)
    if client_uuid:
        ApiIdempotencyKey.objects.create(
            client_uuid=client_uuid, action=COMPLETE_ACTION, target_id=work_order.pk
        )
    return work_order


def _as_conflict(error):
    """Un `client_uuid` ya usado en otra operación es 409 también en la API v1 (como fotos y OT)."""
    if getattr(error, "code", None) == ERROR_CONFLICT:
        return Conflict(error.messages[0])
    return error


def add_part_usage_idempotent(*, membership, work_order: WorkOrder, **data) -> tuple[PartUsage, bool]:
    """Repuesto usado en la OT desde la API (INV-09, INV-13); devuelve (línea, creada).

    El mismo servicio que la web (`work.services.add_part_usage`): con `part` descuenta
    stock y reserva al costo promedio vigente. Reenviar el mismo `client_uuid` devuelve
    la línea ya creada (también con la OT ya cerrada); usarlo en otra OT u operación → 409.
    """
    require_perm(membership, Perm.WORK_ORDER_EXECUTE)
    client_uuid = data.get("client_uuid")
    replay = bool(client_uuid) and PartUsage.objects.filter(client_uuid=client_uuid).exists()
    try:
        usage = add_part_usage(work_order=work_order, membership=membership, **data)
    except (WorkOrderError, InventoryError) as error:
        raise _as_conflict(error) from error
    return usage, not replay


def record_stock_movement_idempotent(*, membership, kind: str, client_uuid=None, **data) -> tuple[list, bool]:
    """Entrada, conteo o transferencia desde la API v1 (INV-17); devuelve (movimientos, creado).

    Usa los servicios del núcleo de inventario (`receive_stock`, `count_stock`,
    `transfer_stock`), que revisan `client_uuid` antes que nada: reenviarlo devuelve lo
    ya creado y usarlo en otra operación → 409. Un conteo sin diferencia no crea nada.
    """
    replay = bool(client_uuid) and StockMovement.objects.filter(client_uuid=client_uuid).exists()
    part = data.pop("part")
    to_warehouse = data.pop("to_warehouse", None)
    try:
        if kind == "receipt":
            fields = ("quantity", "warehouse", "unit_cost", "reference", "supplier", "note", "occurred_at")
            movements = [
                inventory.receive_stock(
                    membership=membership,
                    part=part,
                    client_uuid=client_uuid,
                    **{name: data[name] for name in fields if name in data},
                )
            ]
        elif kind == "count":
            fields = ("warehouse", "unit_cost", "note", "occurred_at")
            movement = inventory.count_stock(
                membership=membership,
                part=part,
                counted_quantity=data["counted_quantity"],
                client_uuid=client_uuid,
                **{name: data[name] for name in fields if name in data},
            )
            movements = [movement] if movement is not None else []
        elif kind == "transfer":
            movements = list(
                inventory.transfer_stock(
                    membership=membership,
                    part=part,
                    from_warehouse=data["warehouse"],
                    to_warehouse=to_warehouse,
                    quantity=data["quantity"],
                    note=data.get("note", ""),
                    client_uuid=client_uuid,
                )
            )
        else:
            raise ValueError(f"Tipo de movimiento no válido: {kind}")
    except InventoryError as error:
        raise _as_conflict(error) from error
    return movements, bool(movements) and not replay


def _same_answer(item, *, result, value_number, value_text, signature, note) -> bool:
    """¿El ítem ya tiene exactamente esta respuesta? (reenvío desde un cliente sin conexión)"""
    if not item.result or item.note != (note or "")[:500]:
        return False
    if item.item_type in ("signature", "photo"):
        return True  # una firma ya guardada no se duplica; la foto ya es la respuesta
    if item.item_type == "measurement":
        try:
            return item.value_number == parse_measurement(value_number)
        except WorkOrderError:
            return False
    if item.item_type == "text":
        return item.value_text == (value_text or "").strip()
    return item.result == result


def answer_checklist_item_idempotent(
    *,
    membership,
    item,
    result="",
    value_number=None,
    value_text="",
    signature=None,
    note="",
    answered_at=None,
):
    """Respuesta de un ítem desde la API con el servicio de la web; repetirla no cambia nada.

    `answered_at` (hora en que se respondió sin conexión): gana la última; una respuesta
    más antigua que la vigente vuelve con `superseded=True` sin aplicarse.
    """
    require_perm(membership, Perm.WORK_ORDER_EXECUTE)
    answer = {
        "result": result,
        "value_number": value_number,
        "value_text": value_text,
        "signature": signature,
        "note": note,
    }
    if _same_answer(item, **answer):
        return item
    return answer_checklist_item(item=item, membership=membership, answered_at=answered_at, **answer)


def _replayed_photo(*, client_uuid, target_type: str, target_id) -> Attachment | None:
    """La foto ya subida con este `client_uuid`, o None. Si era de otro objeto, es un conflicto."""
    if not client_uuid:
        return None
    existing = Attachment.objects.filter(client_uuid=client_uuid).first()
    if existing is None:
        return None
    if (existing.target_type, existing.target_id) != (target_type, target_id):
        raise Conflict(_("Este client_uuid ya se usó para otra operación."))
    return existing


def upload_work_order_photo_idempotent(
    *, membership, work_order: WorkOrder, uploaded_file, client_uuid=None, taken_at=None
) -> tuple[Attachment, bool]:
    """Foto de la OT desde la API con el servicio de la web; devuelve (foto, creada).

    Reenviar el mismo `client_uuid` devuelve la foto ya subida (también con la OT ya completada).
    """
    require_perm(membership, Perm.WORK_ORDER_EXECUTE)
    existing = _replayed_photo(
        client_uuid=client_uuid, target_type=AttachmentTargetType.WORK_ORDER, target_id=work_order.pk
    )
    if existing is not None:
        return existing, False
    photo = add_work_order_photo(
        work_order=work_order,
        membership=membership,
        uploaded_file=uploaded_file,
        client_uuid=client_uuid,
        taken_at=taken_at,
    )
    return photo, True


def upload_checklist_photo_idempotent(
    *, membership, item, uploaded_file, client_uuid=None, taken_at=None
) -> tuple[Attachment, bool]:
    """Foto de un ítem del checklist desde la API (`add_checklist_photo`); devuelve (foto, creada).

    En un ítem «foto obligatoria» la foto es la respuesta: el ítem queda hecho.
    """
    require_perm(membership, Perm.WORK_ORDER_EXECUTE)
    existing = _replayed_photo(
        client_uuid=client_uuid, target_type=AttachmentTargetType.CHECKLIST_ITEM, target_id=item.pk
    )
    if existing is not None:
        return existing, False
    photo = add_checklist_photo(
        item=item,
        membership=membership,
        uploaded_file=uploaded_file,
        client_uuid=client_uuid,
        taken_at=taken_at,
    )
    return photo, True
