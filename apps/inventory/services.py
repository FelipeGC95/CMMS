"""Reglas de negocio de inventario (INV, ADR-028).

El libro (`StockMovement`) manda y el saldo (`StockItem`) se actualiza en la
misma transacción que cada movimiento o reserva. Concurrencia: todo pasa en
`transaction.atomic` y bloquea primero los repuestos (en orden de pk) y luego
sus saldos (en orden de `warehouse_id`); con el repuesto bloqueado se calcula el
costo promedio y el saldo resultante, y el saldo se escribe con `F()`.

Idempotencia (móvil y API): `client_uuid` se revisa ANTES de las validaciones de
estado; reenviar devuelve lo ya creado y usarlo en otra operación es un conflicto.
"""

import uuid
from collections import defaultdict
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import F, Sum
from django.utils import timezone
from django.utils.translation import gettext as _

from apps.accounts.permissions import Perm, require_perm
from apps.accounts.scoping import allowed_location_ids
from apps.audit import services as audit
from apps.audit.models import AuditEvent
from apps.core.models import uuid7
from apps.notifications.services import notify_stock_low
from apps.organizations.models import Location
from apps.webhooks.models import Event as WebhookEvent
from apps.webhooks.services import emit_event

from .models import Part, Reservation, StockItem, StockMovement, Warehouse
from .selectors import can_see_warehouse, warehouse_for

Kind = StockMovement.Kind
Reason = StockMovement.Reason

QUANTITY_STEP = Decimal("0.001")
COST_STEP = Decimal("0.0001")
MONEY_STEP = Decimal("0.01")
ZERO = Decimal(0)
# Un conteo exige motivo si la diferencia es negativa o supera este porcentaje (INV-06).
COUNT_REASON_THRESHOLD = Decimal("0.10")
DEFAULT_WAREHOUSE_CODE = "BOD"

PART_FIELDS = (
    "code",
    "name",
    "unit",
    "category",
    "description",
    "manufacturer",
    "manufacturer_ref",
    "supplier",
    "notes",
)
WAREHOUSE_FIELDS = ("code", "name", "location", "notes")

# Mismos códigos que `WorkOrderError` (la API los traduce a 409, X-06).
ERROR_CANCELLED = "cancelled"
ERROR_LOCKED = "locked"
ERROR_CONFLICT = "conflict"


class InventoryError(ValidationError):
    """Regla de inventario incumplida. `code`: `conflict`, `cancelled`, `locked` o None."""


# --- Valores -------------------------------------------------------------------


def _decimal(value, *, field: str) -> Decimal:
    try:
        number = Decimal(str(value).strip().replace(",", "."))
    except (InvalidOperation, ValueError, TypeError):
        raise InventoryError({field: _("Ingresa un número válido.")}) from None
    if not number.is_finite():
        raise InventoryError({field: _("Ingresa un número válido.")})
    return number


def _quantity(value, *, field: str = "quantity", positive: bool = True) -> Decimal:
    number = _decimal(value, field=field).quantize(QUANTITY_STEP, rounding=ROUND_HALF_UP)
    if positive and number <= 0:
        raise InventoryError({field: _("La cantidad debe ser mayor que cero.")})
    if number < 0:
        raise InventoryError({field: _("La cantidad no puede ser negativa.")})
    return number


def _unit_cost(value) -> Decimal | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    number = _decimal(value, field="unit_cost").quantize(COST_STEP, rounding=ROUND_HALF_UP)
    if number < 0:
        raise InventoryError({"unit_cost": _("El costo no puede ser negativo.")})
    return number


def _money(value: Decimal) -> Decimal:
    return value.quantize(MONEY_STEP, rounding=ROUND_HALF_UP)


def _client_time(at, *, not_before=None):
    from apps.work.services import client_time  # apps.work usa este módulo (P3): import diferido

    return client_time(at, not_before=not_before)


def _clean_text(data: dict, model) -> dict:
    """Quita espacios y valida largos; los errores van por campo."""
    cleaned, errors = {}, {}
    for name, value in data.items():
        model_field = model._meta.get_field(name)
        if model_field.is_relation:
            cleaned[name] = value
            continue
        text = (value or "").strip()
        if model_field.max_length and len(text) > model_field.max_length:
            errors[name] = _("Máximo %(n)s caracteres.") % {"n": model_field.max_length}
        cleaned[name] = text
    if errors:
        raise InventoryError(errors)
    return cleaned


def _require_unique_code(model, code: str, *, exclude_pk=None) -> None:
    """Código único por organización sin distinguir mayúsculas (error en el campo)."""
    qs = model.objects.filter(code__iexact=code)
    if exclude_pk is not None:
        qs = qs.exclude(pk=exclude_pk)
    if qs.exists():
        raise InventoryError({"code": _("Ya existe otro con el código %(code)s.") % {"code": code}})


# --- Bloqueos y saldo ----------------------------------------------------------


def _lock_parts(part_ids) -> dict:
    """Bloquea los repuestos en orden de pk (siempre primero, para no interbloquear)."""
    ids = {getattr(part_id, "pk", part_id) for part_id in part_ids}
    parts = {part.pk: part for part in Part.objects.select_for_update().filter(pk__in=ids).order_by("pk")}
    if len(parts) != len(ids):
        raise InventoryError(_("Repuesto no encontrado."))
    return parts


def _lock_part(part) -> Part:
    return _lock_parts([part.pk])[part.pk]


def _lock_stock_items(part, warehouses) -> dict:
    """Saldos del repuesto en esas bodegas, bloqueados en orden de `warehouse_id`; crea los que falten.

    Llamar con el repuesto ya bloqueado: así dos transacciones nunca crean el mismo saldo.
    """
    by_id = {warehouse.pk: warehouse for warehouse in warehouses}
    items = {
        item.warehouse_id: item
        for item in StockItem.objects.select_for_update()
        .filter(part=part, warehouse_id__in=list(by_id))
        .order_by("warehouse_id")
    }
    for warehouse_id in sorted(set(by_id) - set(items)):
        items[warehouse_id] = StockItem.objects.create(part=part, warehouse=by_id[warehouse_id])
    for item in items.values():
        item.warehouse = by_id[item.warehouse_id]
    return items


def _total_on_hand(part) -> Decimal:
    """Q de INV-08: existencia del repuesto en todas las bodegas."""
    return StockItem.objects.filter(part=part).aggregate(total=Sum("on_hand"))["total"] or ZERO


def _weighted_average(part, *, quantity: Decimal, unit_cost: Decimal) -> Decimal:
    """INV-08: Q ≤ 0 → c; si no, (Q·prom + q·c) / (Q + q). Con el repuesto bloqueado."""
    total = _total_on_hand(part)
    if total <= 0:
        return unit_cost
    return ((total * part.avg_cost + quantity * unit_cost) / (total + quantity)).quantize(
        COST_STEP, rounding=ROUND_HALF_UP
    )


def _record(
    *,
    part,
    item,
    kind,
    quantity: Decimal,
    by,
    occurred_at,
    unit_cost: Decimal | None = None,
    revalue: bool = False,
    reason: str = "",
    work_order=None,
    reverses=None,
    transfer_group=None,
    reference: str = "",
    supplier: str = "",
    note: str = "",
    client_uuid=None,
) -> StockMovement:
    """Crea el movimiento y aplica su efecto al saldo y al promedio (repuesto y saldo bloqueados).

    `revalue`: entrada con costo que recalcula el promedio; si no, se valoriza al promedio vigente.
    """
    if revalue:
        applied_cost = unit_cost
        new_avg = _weighted_average(part, quantity=quantity, unit_cost=unit_cost)
    else:
        applied_cost = part.avg_cost
        new_avg = part.avg_cost
    on_hand_after = item.on_hand + quantity
    movement = StockMovement.objects.create(
        part=part,
        warehouse=item.warehouse,
        kind=kind,
        reason=reason,
        quantity=quantity,
        unit_cost=applied_cost,
        total_cost=_money(quantity * applied_cost),
        avg_cost_after=new_avg,
        on_hand_after=on_hand_after,
        work_order=work_order,
        reverses=reverses,
        transfer_group=transfer_group,
        reference=reference.strip()[:60],
        supplier=supplier.strip()[:150],
        note=note.strip()[:500],
        occurred_at=occurred_at,
        created_by=by,
        client_uuid=client_uuid,
    )
    emit_event(event_type=WebhookEvent.STOCK_MOVEMENT_CREATED, instance=movement)  # X-08
    now = timezone.now()
    StockItem.objects.filter(pk=item.pk).update(
        on_hand=F("on_hand") + quantity, last_movement_at=now, updated_at=now
    )
    item.on_hand, item.last_movement_at = on_hand_after, now
    if new_avg != part.avg_cost:
        part.avg_cost = new_avg
        part.save(update_fields=["avg_cost", "updated_at"])
    return movement


def _change_reserved(item, delta: Decimal) -> None:
    if delta:
        StockItem.objects.filter(pk=item.pk).update(reserved=F("reserved") + delta, updated_at=timezone.now())
        item.reserved += delta


def _below_min(items) -> dict:
    return {item.pk: item.is_below_min for item in items}


def _alert_crossings(items, before: dict, *, actor=None) -> None:
    """Aviso `stock_low` y evento `part.below_minimum` al pasar de no-bajo a bajo mínimo (INV-12 CA3,
    X-08): una vez por episodio, al confirmar."""
    for item in items:
        if item.is_below_min and not before.get(item.pk, False):
            notify_stock_low(stock_item=item, actor=actor)
            emit_event(event_type=WebhookEvent.PART_BELOW_MINIMUM, instance=item)


def _existing_movement(client_uuid, *, kind, part=None, work_order=None):
    """Movimiento ya creado con esa clave (reenvío) o conflicto si fue otra operación."""
    if not client_uuid:
        return None
    existing = StockMovement.objects.filter(client_uuid=client_uuid).first()
    if existing is None:
        return None
    if (
        existing.kind != kind
        or (part is not None and existing.part_id != part.pk)
        or (work_order is not None and existing.work_order_id != work_order.pk)
    ):
        raise InventoryError(_("Este client_uuid ya se usó para otra operación."), code=ERROR_CONFLICT)
    return existing


def _audit_movement(action, movement, *, actor, **extra) -> None:
    changes = {
        "movement": str(movement.pk),
        "kind": movement.kind,
        "warehouse": movement.warehouse.code,
        "quantity": movement.quantity,
        "unit_cost": movement.unit_cost,
        "on_hand_after": movement.on_hand_after,
        "avg_cost_after": movement.avg_cost_after,
    }
    if movement.work_order_id:
        changes["work_order"] = str(movement.work_order_id)
    changes.update(extra)
    audit.record(action=action, target=movement.part, actor=actor, changes=changes)


def _require_parts_editable(work_order) -> None:
    """Consumos y devoluciones: la OT no puede estar cerrada ni cancelada (como sus costos, WO-04)."""
    from apps.work.models import WorkOrder

    if work_order.status == WorkOrder.Status.CANCELLED:
        raise InventoryError(_("La OT está cancelada: no admite repuestos."), code=ERROR_CANCELLED)
    if work_order.status == WorkOrder.Status.CLOSED:
        raise InventoryError(
            _("La OT está cerrada: sus repuestos ya no se pueden cambiar."), code=ERROR_LOCKED
        )


# --- Bodegas -------------------------------------------------------------------


def ensure_default_warehouse() -> Warehouse:
    """La bodega por defecto; si no hay, se crea sola "Bodega principal" (INV-03 CA2, diferida)."""
    existing = Warehouse.objects.filter(is_default=True).first()
    if existing is not None:
        return existing
    with transaction.atomic():
        candidate = (
            Warehouse.objects.select_for_update().filter(is_active=True).order_by("created_at").first()
            or Warehouse.objects.select_for_update().filter(code__iexact=DEFAULT_WAREHOUSE_CODE).first()
        )
        try:
            with transaction.atomic():
                if candidate is not None:
                    candidate.is_default, candidate.is_active = True, True
                    candidate.save(update_fields=["is_default", "is_active", "updated_at"])
                    warehouse = candidate
                else:
                    warehouse = Warehouse.objects.create(
                        code=DEFAULT_WAREHOUSE_CODE, name=_("Bodega principal"), is_default=True
                    )
        except IntegrityError:  # otra transacción la creó al mismo tiempo
            return Warehouse.objects.get(is_default=True)
        audit.record(
            action=AuditEvent.Action.CREATE if candidate is None else AuditEvent.Action.UPDATE,
            target=warehouse,
            changes={"fields": audit.snapshot(warehouse), "automatic": True},
        )
    return warehouse


def _get_warehouse(warehouse, *, membership=None, active=True) -> Warehouse:
    """Bodega de la organización del contexto, visible para la membresía (INV-03 CA6)."""
    pk = getattr(warehouse, "pk", warehouse)
    found = Warehouse.objects.filter(pk=pk).select_related("location").first()
    if found is None or (membership is not None and not can_see_warehouse(membership, found)):
        raise InventoryError({"warehouse": _("Bodega no encontrada.")})
    if active and not found.is_active:
        raise InventoryError({"warehouse": _("La bodega %(code)s está desactivada.") % {"code": found.code}})
    return found


def _resolve_warehouse(membership, warehouse) -> Warehouse:
    if warehouse is None:
        warehouse = ensure_default_warehouse()
    return _get_warehouse(warehouse, membership=membership)


def _require_location_in_scope(membership, location) -> None:
    if location is None:
        return
    if not Location.objects.filter(pk=location.pk).exists():
        raise InventoryError({"location": _("Ubicación no encontrada.")})
    allowed = allowed_location_ids(membership)
    if allowed is not None and location.pk not in allowed:
        raise InventoryError({"location": _("Ubicación no encontrada.")})


@transaction.atomic
def create_warehouse(*, membership, code, name, location=None, notes="") -> Warehouse:
    """Nueva bodega (INV-03). Si aún no hay bodega por defecto, esta pasa a serlo."""
    require_perm(membership, Perm.INVENTORY_MANAGE)
    data = _clean_text({"code": code, "name": name, "notes": notes}, Warehouse)
    if not data["code"] or not data["name"]:
        raise InventoryError(
            {key: _("Este campo es obligatorio.") for key in ("code", "name") if not data[key]}
        )
    _require_unique_code(Warehouse, data["code"])
    _require_location_in_scope(membership, location)
    is_default = not Warehouse.objects.filter(is_default=True).exists()
    warehouse = Warehouse.objects.create(**data, location=location, is_default=is_default)
    audit.record(
        action=AuditEvent.Action.CREATE,
        target=warehouse,
        membership=membership,
        changes={"fields": audit.snapshot(warehouse)},
    )
    return warehouse


@transaction.atomic
def update_warehouse(*, membership, warehouse, **fields) -> Warehouse:
    """Edita código, nombre, ubicación o notas (INV-03)."""
    require_perm(membership, Perm.INVENTORY_MANAGE)
    unknown = set(fields) - set(WAREHOUSE_FIELDS)
    if unknown:
        raise TypeError(f"Campos no editables: {sorted(unknown)}")
    warehouse = _get_warehouse(warehouse, membership=membership, active=False)
    data = _clean_text(fields, Warehouse)
    if "code" in data:
        if not data["code"]:
            raise InventoryError({"code": _("Este campo es obligatorio.")})
        _require_unique_code(Warehouse, data["code"], exclude_pk=warehouse.pk)
    if "name" in data and not data["name"]:
        raise InventoryError({"name": _("Este campo es obligatorio.")})
    if "location" in data:
        _require_location_in_scope(membership, data["location"])
    before = audit.snapshot(warehouse)
    for name, value in data.items():
        setattr(warehouse, name, value)
    warehouse.save()
    changes = audit.diff(before, audit.snapshot(warehouse))
    if changes:
        audit.record(
            action=AuditEvent.Action.UPDATE,
            target=warehouse,
            membership=membership,
            changes={"fields": changes},
        )
    return warehouse


@transaction.atomic
def set_default_warehouse(*, membership, warehouse) -> Warehouse:
    """Cambia la bodega por defecto (siempre exactamente una, activa)."""
    require_perm(membership, Perm.INVENTORY_MANAGE)
    warehouse = _get_warehouse(warehouse, membership=membership)
    current = Warehouse.objects.select_for_update().filter(is_default=True).first()
    if current is not None and current.pk == warehouse.pk:
        return current
    if current is not None:
        current.is_default = False
        current.save(update_fields=["is_default", "updated_at"])
    warehouse.is_default = True
    warehouse.save(update_fields=["is_default", "updated_at"])
    audit.record(
        action=AuditEvent.Action.UPDATE,
        target=warehouse,
        membership=membership,
        changes={
            "fields": {"is_default": [False, True]},
            "previous_default": str(current.pk) if current else "",
        },
    )
    return warehouse


@transaction.atomic
def set_warehouse_active(*, membership, warehouse, is_active: bool) -> Warehouse:
    """Desactiva o reactiva. No se desactiva la por defecto ni una con stock o reservas (INV-03)."""
    require_perm(membership, Perm.INVENTORY_MANAGE)
    warehouse = _get_warehouse(warehouse, membership=membership, active=False)
    warehouse = Warehouse.objects.select_for_update().get(pk=warehouse.pk)
    if warehouse.is_active == is_active:
        return warehouse
    if not is_active:
        if warehouse.is_default:
            raise InventoryError(
                _("La bodega por defecto no se puede desactivar: elige primero otra bodega por defecto.")
            )
        if StockItem.objects.filter(warehouse=warehouse).exclude(on_hand=0).exists():
            raise InventoryError(
                _("La bodega tiene stock: transfiérelo o ajústalo a cero antes de desactivarla.")
            )
        if (
            Reservation.objects.filter(warehouse=warehouse, status=Reservation.Status.ACTIVE).exists()
            or StockItem.objects.filter(warehouse=warehouse).exclude(reserved=0).exists()
        ):
            raise InventoryError(_("La bodega tiene reservas activas: libéralas antes de desactivarla."))
    warehouse.is_active = is_active
    warehouse.save(update_fields=["is_active", "updated_at"])
    audit.record(
        action=AuditEvent.Action.REACTIVATE if is_active else AuditEvent.Action.DEACTIVATE,
        target=warehouse,
        membership=membership,
        changes={"fields": {"is_active": [not is_active, is_active]}},
    )
    return warehouse


# --- Catálogo ------------------------------------------------------------------


def _get_part(part) -> Part:
    found = Part.objects.filter(pk=getattr(part, "pk", part)).first()
    if found is None:
        raise InventoryError({"part": _("Repuesto no encontrado.")})
    return found


@transaction.atomic
def create_part(
    *,
    membership,
    code,
    name,
    unit="un",
    category="",
    description="",
    manufacturer="",
    manufacturer_ref="",
    supplier="",
    notes="",
) -> Part:
    """Alta de repuesto (INV-01): código único por organización sin distinguir mayúsculas."""
    require_perm(membership, Perm.INVENTORY_MANAGE)
    data = _clean_text(
        {
            "code": code,
            "name": name,
            "unit": unit,
            "category": category,
            "description": description,
            "manufacturer": manufacturer,
            "manufacturer_ref": manufacturer_ref,
            "supplier": supplier,
            "notes": notes,
        },
        Part,
    )
    if not data["code"] or not data["name"]:
        raise InventoryError(
            {key: _("Este campo es obligatorio.") for key in ("code", "name") if not data[key]}
        )
    data["unit"] = data["unit"] or "un"
    _require_unique_code(Part, data["code"])
    part = Part.objects.create(**data, created_by=membership.user)
    audit.record(
        action=AuditEvent.Action.CREATE,
        target=part,
        membership=membership,
        changes={"fields": audit.snapshot(part)},
    )
    return part


@transaction.atomic
def update_part(*, membership, part, **fields) -> Part:
    """Edita los datos del catálogo (no el costo ni el estado, que tienen su propio camino)."""
    require_perm(membership, Perm.INVENTORY_MANAGE)
    unknown = set(fields) - set(PART_FIELDS)
    if unknown:
        raise TypeError(f"Campos no editables: {sorted(unknown)}")
    part = _get_part(part)
    data = _clean_text(fields, Part)
    for key in ("code", "name"):
        if key in data and not data[key]:
            raise InventoryError({key: _("Este campo es obligatorio.")})
    if "unit" in data:
        data["unit"] = data["unit"] or "un"
    if "code" in data:
        _require_unique_code(Part, data["code"], exclude_pk=part.pk)
    before = audit.snapshot(part)
    for name, value in data.items():
        setattr(part, name, value)
    part.save(update_fields=[*data, "updated_at"])
    changes = audit.diff(before, audit.snapshot(part))
    if changes:
        audit.record(
            action=AuditEvent.Action.UPDATE, target=part, membership=membership, changes={"fields": changes}
        )
    return part


@transaction.atomic
def set_part_active(*, membership, part, is_active: bool) -> Part:
    """Desactiva (desaparece de buscadores; historial visible) o reactiva un repuesto."""
    require_perm(membership, Perm.INVENTORY_MANAGE)
    part = _get_part(part)
    if part.is_active == is_active:
        return part
    part.is_active = is_active
    part.save(update_fields=["is_active", "updated_at"])
    audit.record(
        action=AuditEvent.Action.REACTIVATE if is_active else AuditEvent.Action.DEACTIVATE,
        target=part,
        membership=membership,
        changes={"fields": {"is_active": [not is_active, is_active]}},
    )
    return part


@transaction.atomic
def set_stock_levels(
    *, membership, part, warehouse=None, min_quantity, target_quantity=None, bin_location=""
) -> StockItem:
    """Mínimo, objetivo y ubicación en bodega de un repuesto en una bodega (INV-12)."""
    require_perm(membership, Perm.INVENTORY_MANAGE)
    minimum = _quantity(min_quantity, field="min_quantity", positive=False)
    target = None
    if target_quantity is not None and str(target_quantity).strip() != "":
        target = _quantity(target_quantity, field="target_quantity", positive=False)
        if target < minimum:
            raise InventoryError({"target_quantity": _("El objetivo no puede ser menor que el mínimo.")})
    bin_location = (bin_location or "").strip()
    if len(bin_location) > 40:
        raise InventoryError({"bin_location": _("Máximo %(n)s caracteres.") % {"n": 40}})
    warehouse = _resolve_warehouse(membership, warehouse)
    part = _lock_part(_get_part(part))
    item = _lock_stock_items(part, [warehouse])[warehouse.pk]
    before = audit.snapshot(item, fields=["min_quantity", "target_quantity", "bin_location"])
    item.min_quantity, item.target_quantity, item.bin_location = minimum, target, bin_location
    item.save(update_fields=["min_quantity", "target_quantity", "bin_location", "updated_at"])
    changes = audit.diff(
        before, audit.snapshot(item, fields=["min_quantity", "target_quantity", "bin_location"])
    )
    if changes:
        audit.record(
            action=AuditEvent.Action.UPDATE,
            target=part,
            membership=membership,
            changes={"stock_levels": {"warehouse": warehouse.code, **changes}},
        )
    return item


# --- Movimientos ---------------------------------------------------------------


@transaction.atomic
def receive_stock(
    *,
    membership,
    part,
    quantity,
    warehouse=None,
    unit_cost=None,
    reference="",
    supplier="",
    note="",
    occurred_at=None,
    client_uuid=None,
) -> StockMovement:
    """Entrada (INV-05). Con costo recalcula el promedio (INV-08); sin costo, entra al promedio vigente."""
    require_perm(membership, Perm.INVENTORY_MOVE)
    existing = _existing_movement(client_uuid, kind=Kind.RECEIPT, part=part)
    if existing is not None:
        return existing
    quantity = _quantity(quantity)
    cost = _unit_cost(unit_cost)
    warehouse = _resolve_warehouse(membership, warehouse)
    part = _lock_part(_get_part(part))
    item = _lock_stock_items(part, [warehouse])[warehouse.pk]
    at = _client_time(occurred_at)
    movement = _record(
        part=part,
        item=item,
        kind=Kind.RECEIPT,
        quantity=quantity,
        unit_cost=cost,
        revalue=cost is not None,
        by=membership.user,
        occurred_at=at,
        reference=reference,
        supplier=supplier,
        note=note,
        client_uuid=client_uuid,
    )
    part.last_received_at = at
    update_fields = ["last_received_at", "updated_at"]
    if cost is not None:
        part.last_cost = cost
        update_fields.append("last_cost")
    part.save(update_fields=update_fields)
    _audit_movement(
        AuditEvent.Action.STOCK_RECEIVE, movement, actor=membership.user, reference=movement.reference
    )
    return movement


def _count_needs_reason(on_hand: Decimal, difference: Decimal) -> bool:
    if difference < 0:
        return True
    return on_hand != 0 and abs(difference) > abs(on_hand) * COUNT_REASON_THRESHOLD


@transaction.atomic
def count_stock(
    *,
    membership,
    part,
    counted_quantity,
    warehouse=None,
    note="",
    unit_cost=None,
    reason=Reason.COUNT,
    occurred_at=None,
    client_uuid=None,
) -> StockMovement | None:
    """Ajuste por conteo (INV-06): ajusta la diferencia; sin diferencia no crea movimiento (None).

    Se valoriza al promedio vigente; un costo informado fija el promedio solo si la
    existencia total del repuesto es ≤ 0 (INV-08). El motivo (`note`) es obligatorio
    en un conteo si la diferencia es negativa o supera el 10 %.
    """
    require_perm(membership, Perm.INVENTORY_MANAGE)
    existing = _existing_movement(client_uuid, kind=Kind.ADJUSTMENT, part=part)
    if existing is not None:
        return existing
    if reason not in (Reason.COUNT, Reason.IMPORT, Reason.INITIAL):
        raise ValueError(f"Motivo de conteo no válido: {reason}")
    counted = _quantity(counted_quantity, field="counted_quantity", positive=False)
    cost = _unit_cost(unit_cost)
    warehouse = _resolve_warehouse(membership, warehouse)
    part = _lock_part(_get_part(part))
    item = _lock_stock_items(part, [warehouse])[warehouse.pk]
    previous = item.on_hand
    difference = counted - previous
    if difference == 0:
        return None
    if reason == Reason.COUNT and not note.strip() and _count_needs_reason(previous, difference):
        raise InventoryError({"note": _("Indica el motivo: la diferencia es negativa o supera el 10 %.")})
    revalue = cost is not None and difference > 0 and _total_on_hand(part) <= 0
    before = _below_min([item])
    movement = _record(
        part=part,
        item=item,
        kind=Kind.ADJUSTMENT,
        reason=reason,
        quantity=difference,
        unit_cost=cost if revalue else None,
        revalue=revalue,
        by=membership.user,
        occurred_at=_client_time(occurred_at),
        note=note,
        client_uuid=client_uuid,
    )
    _alert_crossings([item], before, actor=membership.user)
    _audit_movement(
        AuditEvent.Action.STOCK_ADJUST,
        movement,
        actor=membership.user,
        reason=reason,
        previous=previous,
        counted=counted,
    )
    return movement


@transaction.atomic
def transfer_stock(
    *, membership, part, from_warehouse, to_warehouse, quantity, note="", client_uuid=None
) -> tuple[StockMovement, StockMovement]:
    """Transferencia entre bodegas (INV-07): dos movimientos enlazados; no cambia el promedio."""
    require_perm(membership, Perm.INVENTORY_MOVE)
    existing = _existing_movement(client_uuid, kind=Kind.TRANSFER_OUT, part=part)
    if existing is not None:
        return existing, StockMovement.objects.get(
            transfer_group=existing.transfer_group, kind=Kind.TRANSFER_IN
        )
    quantity = _quantity(quantity)
    source = _get_warehouse(from_warehouse, membership=membership)
    target = _get_warehouse(to_warehouse, membership=membership)
    if source.pk == target.pk:
        raise InventoryError({"to_warehouse": _("Elige una bodega de destino distinta a la de origen.")})
    part = _lock_part(_get_part(part))
    items = _lock_stock_items(part, [source, target])
    before = _below_min(items.values())
    group, at = uuid7(), timezone.now()
    common = {"part": part, "by": membership.user, "occurred_at": at, "transfer_group": group, "note": note}
    out = _record(
        item=items[source.pk], kind=Kind.TRANSFER_OUT, quantity=-quantity, client_uuid=client_uuid, **common
    )
    inbound = _record(item=items[target.pk], kind=Kind.TRANSFER_IN, quantity=quantity, **common)
    _alert_crossings(items.values(), before, actor=membership.user)
    _audit_movement(
        AuditEvent.Action.STOCK_TRANSFER,
        out,
        actor=membership.user,
        to_warehouse=target.code,
        transfer_group=str(group),
    )
    return out, inbound


# --- OT: consumo, devolución y reservas ---------------------------------------


def _active_reservation(work_order, part):
    return (
        Reservation.objects.select_for_update()
        .filter(work_order=work_order, part=part, status=Reservation.Status.ACTIVE)
        .select_related("warehouse")
        .first()
    )


def _consume(reservation, quantity: Decimal, item) -> None:
    """Descuenta un consumo de la reserva y del reservado; al cubrirla queda `fulfilled`."""
    covered = min(reservation.pending, quantity)
    reservation.consumed_quantity += quantity
    if reservation.consumed_quantity >= reservation.quantity:
        reservation.status = Reservation.Status.FULFILLED
    reservation.save(update_fields=["consumed_quantity", "status", "updated_at"])
    _change_reserved(item, -covered)


@transaction.atomic
def issue_to_work_order(
    *, work_order, part, quantity, by, warehouse=None, occurred_at=None, client_uuid=None
) -> StockMovement:
    """Salida a una OT (INV-09) al costo promedio vigente; descuenta la reserva si la hay.

    Primitiva sin permisos: la llama `work.services.add_part_usage` (que exige
    `work_order.execute`). Nunca se rechaza por falta de stock (Q1). Bodega:
    `warehouse_for` (reserva → ancestro más cercano → por defecto).
    """
    existing = _existing_movement(client_uuid, kind=Kind.ISSUE, part=part, work_order=work_order)
    if existing is not None:
        return existing
    _require_parts_editable(work_order)
    quantity = _quantity(quantity)
    if warehouse is None:
        warehouse = warehouse_for(work_order=work_order, part=part)
    warehouse = _get_warehouse(warehouse)
    part = _lock_part(_get_part(part))
    reservation = _active_reservation(work_order, part)
    warehouses = [warehouse] + ([reservation.warehouse] if reservation is not None else [])
    items = _lock_stock_items(part, warehouses)
    before = _below_min(items.values())
    movement = _record(
        part=part,
        item=items[warehouse.pk],
        kind=Kind.ISSUE,
        quantity=-quantity,
        by=by,
        occurred_at=_client_time(occurred_at, not_before=work_order.created_at),
        work_order=work_order,
        client_uuid=client_uuid,
    )
    if reservation is not None:
        _consume(reservation, quantity, items[reservation.warehouse_id])
    _alert_crossings(items.values(), before, actor=by)
    _audit_movement(AuditEvent.Action.STOCK_ISSUE, movement, actor=by)
    return movement


@transaction.atomic
def return_from_work_order(*, movement, by, note="") -> StockMovement:
    """Devolución de una salida (INV-09 CA6): misma cantidad y costo, recalcula el promedio.

    Idempotente: una salida se devuelve una sola vez. No reabre la reserva (INV-10).
    """
    issue = (
        StockMovement.objects.filter(pk=getattr(movement, "pk", movement))
        .select_related("work_order")
        .first()
    )
    if issue is None or issue.kind != Kind.ISSUE:
        raise InventoryError(_("Solo se puede devolver una salida a OT."))
    existing = StockMovement.objects.filter(reverses=issue).first()
    if existing is not None:
        return existing
    _require_parts_editable(issue.work_order)
    part = _lock_part(issue.part)
    existing = StockMovement.objects.filter(reverses=issue).first()  # bajo el bloqueo
    if existing is not None:
        return existing
    warehouse = _get_warehouse(issue.warehouse_id, active=False)
    item = _lock_stock_items(part, [warehouse])[warehouse.pk]
    returned = _record(
        part=part,
        item=item,
        kind=Kind.RETURN,
        quantity=-issue.quantity,
        unit_cost=issue.unit_cost,
        revalue=True,
        by=by,
        occurred_at=timezone.now(),
        work_order=issue.work_order,
        reverses=issue,
        note=note,
    )
    _audit_movement(AuditEvent.Action.STOCK_RETURN, returned, actor=by, reverses=str(issue.pk))
    return returned


@transaction.atomic
def apply_consumption_to_reservation(*, work_order, part, quantity) -> Reservation | None:
    """Descuenta un consumo de la reserva activa del repuesto en la OT (si la hay)."""
    quantity = _quantity(quantity)
    part = _lock_part(_get_part(part))
    reservation = _active_reservation(work_order, part)
    if reservation is None:
        return None
    item = _lock_stock_items(part, [reservation.warehouse])[reservation.warehouse_id]
    _consume(reservation, quantity, item)
    return reservation


@transaction.atomic
def reserve_for_work_order(
    *, work_order, part, quantity, warehouse=None, source, plan_resource=None, by=None
) -> Reservation:
    """Reserva un repuesto para una OT (INV-10). Informa, nunca bloquea; repetidos se suman.

    Primitiva sin permisos: la vista de OT exige `work_order.manage`; la
    generación de preventivas (P4) la llama con `by=None` (sistema).
    """
    from apps.work.models import WorkOrder

    if work_order.status in (WorkOrder.Status.COMPLETED, *WorkOrder.CLOSED_STATUSES):
        code = ERROR_CANCELLED if work_order.status == WorkOrder.Status.CANCELLED else ERROR_LOCKED
        raise InventoryError(_("La OT ya no admite reservas."), code=code)
    if source not in Reservation.Source.values:
        raise ValueError(f"Origen de reserva no válido: {source}")
    quantity = _quantity(quantity)
    part = _lock_part(_get_part(part))
    reservation = _active_reservation(work_order, part)
    if reservation is not None:
        warehouse = reservation.warehouse
    else:
        warehouse = _get_warehouse(warehouse or warehouse_for(work_order=work_order))
    item = _lock_stock_items(part, [warehouse])[warehouse.pk]
    before = _below_min([item])
    if reservation is not None:
        reservation.quantity += quantity
        reservation.save(update_fields=["quantity", "updated_at"])
    else:
        reservation = Reservation.objects.create(
            work_order=work_order,
            part=part,
            warehouse=warehouse,
            quantity=quantity,
            source=source,
            plan_resource=plan_resource,
            created_by=by,
        )
    _change_reserved(item, quantity)
    _alert_crossings([item], before, actor=by)
    audit.record(
        action=AuditEvent.Action.RESERVE,
        target=work_order,
        actor=by,
        changes={
            "reservation": str(reservation.pk),
            "part": part.code,
            "warehouse": warehouse.code,
            "quantity": quantity,
            "source": source,
        },
    )
    return reservation


def _release(reservation_ids, *, reason, by=None) -> int:
    """Libera reservas activas: el pendiente sale del reservado (dentro de una transacción)."""
    rows = list(
        Reservation.objects.filter(pk__in=reservation_ids, status=Reservation.Status.ACTIVE).values_list(
            "part_id", flat=True
        )
    )
    if not rows:
        return 0
    _lock_parts(rows)
    reservations = list(
        Reservation.objects.select_for_update()
        .filter(pk__in=reservation_ids, status=Reservation.Status.ACTIVE)
        .select_related("part", "warehouse", "work_order")
        .order_by("part_id", "warehouse_id")
    )
    by_part = defaultdict(list)
    for reservation in reservations:
        by_part[reservation.part_id].append(reservation)
    now = timezone.now()
    for part_id in sorted(by_part):
        group = by_part[part_id]
        items = _lock_stock_items(group[0].part, [reservation.warehouse for reservation in group])
        for reservation in group:
            pending = reservation.pending
            reservation.status = Reservation.Status.RELEASED
            reservation.release_reason = reason
            reservation.released_at = now
            reservation.save(update_fields=["status", "release_reason", "released_at", "updated_at"])
            _change_reserved(items[reservation.warehouse_id], -pending)
            audit.record(
                action=AuditEvent.Action.RELEASE,
                target=reservation.work_order,
                actor=by,
                changes={
                    "reservation": str(reservation.pk),
                    "part": reservation.part.code,
                    "pending": pending,
                    "reason": reason,
                },
            )
    return len(reservations)


@transaction.atomic
def release_reservation(*, membership, reservation) -> Reservation:
    """Liberación manual de una reserva (INV-10): `work_order.manage` y la OT a la vista."""
    from apps.work.selectors import visible_work_orders

    require_perm(membership, Perm.WORK_ORDER_MANAGE)
    found = Reservation.objects.filter(pk=getattr(reservation, "pk", reservation)).first()
    if (
        found is None
        or not visible_work_orders(membership=membership).filter(pk=found.work_order_id).exists()
    ):
        raise InventoryError(_("Reserva no encontrada."))
    if found.status != Reservation.Status.ACTIVE:
        raise InventoryError(_("La reserva ya no está activa."))
    _release([found.pk], reason=Reservation.ReleaseReason.MANUAL, by=membership.user)
    found.refresh_from_db()
    return found


@transaction.atomic
def release_work_order_reservations(*, work_order, reason, by=None) -> int:
    """Libera las reservas activas de la OT al cerrarla (`closed`) o cancelarla (`cancelled`)."""
    if reason not in Reservation.ReleaseReason.values or not reason:
        raise ValueError(f"Motivo de liberación no válido: {reason}")
    ids = list(
        Reservation.objects.filter(work_order=work_order, status=Reservation.Status.ACTIVE).values_list(
            "pk", flat=True
        )
    )
    return _release(ids, reason=reason, by=by) if ids else 0


# --- Reconciliación (INV-14) ---------------------------------------------------


@dataclass(frozen=True)
class StockMismatch:
    """Diferencia entre el saldo y el libro. `kind`: `on_hand`, `reserved` o `part_usage`."""

    kind: str
    part_id: uuid.UUID
    warehouse_id: uuid.UUID | None
    expected: Decimal
    actual: Decimal
    stock_item_id: uuid.UUID | None = None
    detail: str = ""


def _part_usage_link():
    """`PartUsage.stock_movement` llega con la integración de OT (P3); antes no hay nada que revisar."""
    from apps.work.models import PartUsage

    try:
        PartUsage._meta.get_field("stock_movement")
    except Exception:  # noqa: BLE001 - FieldDoesNotExist hasta P3
        return None
    return PartUsage


def reconcile_stock(*, fix: bool = False) -> list[StockMismatch]:
    """Compara saldos con el libro en la organización del contexto.

    (a) on_hand = Σ movimientos por repuesto y bodega; (b) reserved = Σ pendientes
    de reservas activas; (c) cada consumo enlazado tiene su movimiento con la
    cantidad opuesta. `fix` reescribe (a) y (b) desde el libro y lo audita; (c) solo se informa.
    """
    ledger = {
        (row["part_id"], row["warehouse_id"]): row["total"]
        for row in StockMovement.objects.values("part_id", "warehouse_id").annotate(total=Sum("quantity"))
    }
    pending = defaultdict(lambda: ZERO)
    for part_id, warehouse_id, quantity, consumed in Reservation.objects.filter(
        status=Reservation.Status.ACTIVE
    ).values_list("part_id", "warehouse_id", "quantity", "consumed_quantity"):
        pending[(part_id, warehouse_id)] += max(quantity - consumed, ZERO)
    items = {
        (item["part_id"], item["warehouse_id"]): item
        for item in StockItem.objects.values("id", "part_id", "warehouse_id", "on_hand", "reserved")
    }
    mismatches = []
    for key in sorted(set(ledger) | set(pending) | set(items), key=str):
        item = items.get(key)
        on_hand, reserved = (item["on_hand"], item["reserved"]) if item else (ZERO, ZERO)
        item_id = item["id"] if item else None
        if on_hand != ledger.get(key, ZERO):
            mismatches.append(StockMismatch("on_hand", *key, ledger.get(key, ZERO), on_hand, item_id))
        if reserved != pending.get(key, ZERO):
            mismatches.append(StockMismatch("reserved", *key, pending.get(key, ZERO), reserved, item_id))
    part_usage = _part_usage_link()
    if part_usage is not None:
        for usage_id, part_id, warehouse_id, quantity, moved in part_usage.objects.filter(
            stock_movement__isnull=False
        ).values_list(
            "pk",
            "stock_movement__part_id",
            "stock_movement__warehouse_id",
            "quantity",
            "stock_movement__quantity",
        ):
            if moved != -quantity:
                mismatches.append(
                    StockMismatch("part_usage", part_id, warehouse_id, -quantity, moved, detail=str(usage_id))
                )
    if fix:
        _fix_mismatches([m for m in mismatches if m.kind in ("on_hand", "reserved")])
    return mismatches


@transaction.atomic
def _fix_mismatches(mismatches) -> None:
    if not mismatches:
        return
    parts = _lock_parts({m.part_id for m in mismatches})
    for mismatch in sorted(mismatches, key=lambda m: (str(m.part_id), str(m.warehouse_id))):
        part = parts[mismatch.part_id]
        warehouse = Warehouse.objects.get(pk=mismatch.warehouse_id)
        item = _lock_stock_items(part, [warehouse])[warehouse.pk]
        # Con el repuesto bloqueado se vuelve a leer el libro: nada se movió entre la lectura y el arreglo.
        if mismatch.kind == "on_hand":
            field, current = "on_hand", item.on_hand
            expected = (
                StockMovement.objects.filter(part=part, warehouse=warehouse).aggregate(total=Sum("quantity"))[
                    "total"
                ]
                or ZERO
            )
        else:
            field, current = "reserved", item.reserved
            expected = sum(
                (
                    max(quantity - consumed, ZERO)
                    for quantity, consumed in Reservation.objects.filter(
                        part=part, warehouse=warehouse, status=Reservation.Status.ACTIVE
                    ).values_list("quantity", "consumed_quantity")
                ),
                ZERO,
            )
        if current == expected:
            continue
        StockItem.objects.filter(pk=item.pk).update(**{field: expected, "updated_at": timezone.now()})
        audit.record(
            action=AuditEvent.Action.STOCK_RECONCILE,
            target=part,
            changes={"warehouse": warehouse.code, field: [current, expected]},
        )
