import datetime
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.text import slugify
from django.utils.translation import gettext

from apps.accounts.permissions import Perm, require_perm
from apps.audit import services as audit
from apps.audit.models import AuditEvent

from . import attributes as attribute_values
from .models import (
    Asset,
    AssetClass,
    AssetClassAttribute,
    AssetStatusPeriod,
    AttributeType,
    Meter,
    MeterKind,
    MeterReading,
)

# Más de 24 h de uso por día en un horómetro indica un error de digitación.
MAX_HOURS_PER_DAY = Decimal("24")
USAGE_WINDOW_DAYS = 30


class MeterReadingError(ValidationError):
    pass


@transaction.atomic
def change_asset_status(*, asset: Asset, status: str, by=None, reason: str = "", at=None) -> Asset:
    """Cierra el período de estado vigente y abre uno nuevo."""
    if asset.status == status:
        return asset
    at = at or timezone.now()
    AssetStatusPeriod.objects.filter(asset=asset, ended_at__isnull=True).update(ended_at=at)
    AssetStatusPeriod.objects.create(
        asset=asset, status=status, started_at=at, reason=reason[:200], changed_by=by
    )
    previous = asset.status
    asset.status = status
    asset.save(update_fields=["status", "updated_at"])
    audit.record(
        action=AuditEvent.Action.TRANSITION,
        target=asset,
        actor=by,
        changes={"status": [previous, status], "reason": reason[:200]},
    )
    return asset


@transaction.atomic
def record_meter_reading(
    *,
    meter: Meter,
    value,
    membership=None,
    read_at: datetime.datetime | None = None,
    is_reset: bool = False,
    source: str = MeterReading.Source.MANUAL,
    work_order=None,
    client_uuid=None,
    generate_work_orders: bool = True,
) -> tuple[MeterReading, list[str]]:
    """Registra una lectura (AST-07) y dispara los preventivos por uso que venzan.

    Devuelve la lectura y una lista de advertencias (no bloquean el registro).
    `membership` es None solo en procesos del sistema (importación, API de
    plataforma, datos demo).
    """
    if membership is not None:
        require_perm(membership, Perm.METER_READING_CREATE)
    if client_uuid:
        existing = MeterReading.objects.filter(client_uuid=client_uuid).first()
        if existing:
            return existing, []

    value = Decimal(str(value))
    read_at = read_at or timezone.now()
    if value < 0:
        raise MeterReadingError(gettext("La lectura no puede ser negativa."))

    previous_value, previous_at = meter.current_value, meter.current_read_at
    is_latest = previous_at is None or read_at >= previous_at
    warnings = []
    if meter.is_cumulative and previous_value is not None and is_latest and not is_reset:
        if value < previous_value:
            raise MeterReadingError(
                gettext(
                    "La lectura (%(value)s) es menor que la actual (%(previous)s). "
                    "Si se cambió o reinició el medidor, márcalo."
                )
                % {"value": f"{value.normalize():f}", "previous": f"{previous_value.normalize():f}"}
            )
        elapsed_days = Decimal((read_at - previous_at).total_seconds()) / 86400
        if meter.kind == MeterKind.HOURS and elapsed_days > 0:
            if (value - previous_value) / elapsed_days > MAX_HOURS_PER_DAY:
                warnings.append(
                    gettext("Uso mayor a 24 h por día desde la lectura anterior: revisa el valor.")
                )

    reading = MeterReading.objects.create(
        meter=meter,
        value=value,
        read_at=read_at,
        source=source,
        is_reset=is_reset,
        work_order=work_order,
        created_by=membership.user if membership else None,
        client_uuid=client_uuid,
    )
    audit.record(
        action=AuditEvent.Action.CREATE,
        target=reading,
        membership=membership,
        changes={
            "meter": str(meter.pk),
            "value": value,
            "read_at": read_at,
            "is_reset": is_reset,
            "source": source,
        },
    )
    if not is_latest:
        return reading, warnings  # lectura histórica: no cambia el valor vigente

    meter.current_value = value
    meter.current_read_at = read_at
    meter.avg_daily_usage = _average_daily_usage(meter, until=read_at)
    meter.save(update_fields=["current_value", "current_read_at", "avg_daily_usage", "updated_at"])

    from apps.maintenance import services as maintenance

    if is_reset and previous_value is not None:
        maintenance.shift_meter_targets(meter=meter, delta=value - previous_value)
    if generate_work_orders:
        maintenance.generate_due_work_orders(meter=meter)
    return reading, warnings


def _average_daily_usage(meter: Meter, *, until) -> Decimal | None:
    """Uso por día entre la primera y la última lectura de la ventana, sin cruzar reinicios."""
    since = until - datetime.timedelta(days=USAGE_WINDOW_DAYS)
    readings = list(
        meter.readings.filter(read_at__gte=since, read_at__lte=until)
        .order_by("read_at")
        .values_list("read_at", "value", "is_reset")
    )
    last_reset = max((i for i, r in enumerate(readings) if r[2]), default=0)
    readings = readings[last_reset:]
    if len(readings) < 2:
        return meter.avg_daily_usage
    (first_at, first_value, _), (last_at, last_value, _) = readings[0], readings[-1]
    days = Decimal((last_at - first_at).total_seconds()) / 86400
    if days < 1:
        return meter.avg_daily_usage
    return ((last_value - first_value) / days).quantize(Decimal("0.001"))


# ---------- Clases de activo y sus atributos (AST-01) ----------

CLASS_FIELDS = ["code", "name", "iso_category", "level_hint", "is_active"]
ATTRIBUTE_FIELDS = ["label", "data_type", "unit", "choices", "required", "order", "is_active"]


def _unique_class_code(code, *, exclude=None):
    duplicate = AssetClass.objects.filter(code__iexact=code)
    if exclude is not None:
        duplicate = duplicate.exclude(pk=exclude.pk)
    if duplicate.exists():
        raise ValidationError(gettext("Ya existe una clase con el código %(code)s.") % {"code": code})


@transaction.atomic
def create_asset_class(*, membership, code, name, iso_category, level_hint) -> AssetClass:
    require_perm(membership, Perm.ASSET_MANAGE)
    code = code.strip()
    _unique_class_code(code)
    asset_class = AssetClass.objects.create(
        code=code, name=name.strip(), iso_category=iso_category, level_hint=level_hint
    )
    audit.record(
        action=AuditEvent.Action.CREATE,
        target=asset_class,
        membership=membership,
        changes=audit.snapshot(asset_class, CLASS_FIELDS),
    )
    return asset_class


@transaction.atomic
def update_asset_class(
    *, membership, asset_class, code, name, iso_category, level_hint, is_active
) -> AssetClass:
    require_perm(membership, Perm.ASSET_MANAGE)
    code = code.strip()
    _unique_class_code(code, exclude=asset_class)
    before = audit.snapshot(asset_class, CLASS_FIELDS)
    asset_class.code, asset_class.name = code, name.strip()
    asset_class.iso_category, asset_class.level_hint, asset_class.is_active = (
        iso_category,
        level_hint,
        is_active,
    )
    asset_class.save()
    changes = audit.diff(before, audit.snapshot(asset_class, CLASS_FIELDS))
    if changes:
        audit.record(
            action=AuditEvent.Action.UPDATE, target=asset_class, membership=membership, changes=changes
        )
    return asset_class


def _clean_choices(data_type, choices):
    choices = [c.strip()[:80] for c in choices if c.strip()]
    if data_type == AttributeType.CHOICE and not choices:
        raise ValidationError(gettext("Una lista necesita al menos una opción."))
    return list(dict.fromkeys(choices)) if data_type == AttributeType.CHOICE else []


@transaction.atomic
def add_class_attribute(
    *, membership, asset_class, label, data_type, unit="", choices=(), required=False
) -> AssetClassAttribute:
    """Agrega un atributo a la clase. La clave sale del nombre y no cambia después."""
    require_perm(membership, Perm.ASSET_MANAGE)
    if data_type not in AttributeType.values:
        raise ValidationError(gettext("Tipo de atributo no válido."))
    label = label.strip()
    base = slugify(label).replace("-", "_")[:40] or "atributo"
    existing = set(asset_class.attributes.values_list("key", flat=True))
    key, n = base, 2
    while key in existing:
        key, n = f"{base}_{n}", n + 1
    last = asset_class.attributes.order_by("-order").values_list("order", flat=True).first() or 0
    attribute = AssetClassAttribute.objects.create(
        asset_class=asset_class,
        key=key,
        label=label,
        data_type=data_type,
        unit=unit.strip(),
        choices=_clean_choices(data_type, choices),
        required=required,
        order=last + 1,
    )
    audit.record(
        action=AuditEvent.Action.CREATE,
        target=attribute,
        membership=membership,
        changes=audit.snapshot(attribute, ["key", *ATTRIBUTE_FIELDS]),
    )
    return attribute


@transaction.atomic
def update_class_attribute(
    *, membership, attribute, label, unit, choices, required, order, is_active
) -> AssetClassAttribute:
    """Edita un atributo; tipo y clave quedan fijos y los valores guardados nunca se borran (CA3)."""
    require_perm(membership, Perm.ASSET_MANAGE)
    before = audit.snapshot(attribute, ATTRIBUTE_FIELDS)
    attribute.label, attribute.unit = label.strip(), unit.strip()
    attribute.choices = _clean_choices(attribute.data_type, choices)
    attribute.required, attribute.order, attribute.is_active = required, order, is_active
    attribute.save()
    changes = audit.diff(before, audit.snapshot(attribute, ATTRIBUTE_FIELDS))
    if changes:
        audit.record(
            action=AuditEvent.Action.UPDATE, target=attribute, membership=membership, changes=changes
        )
    return attribute


@transaction.atomic
def set_asset_attributes(*, membership, asset, values: dict) -> Asset:
    """Guarda los valores de los atributos activos de la clase del activo (`values` por clave).

    Los valores de atributos desactivados o de otra clase se conservan (AST-01 CA3).
    """
    require_perm(membership, Perm.ASSET_MANAGE)
    attributes = list(asset.asset_class.attributes.filter(is_active=True))
    stored = dict(asset.attributes)
    errors = []
    for attribute in attributes:
        try:
            value = attribute_values.to_json(attribute, values.get(attribute.key))
        except ValueError as error:
            errors.append(str(error))
            continue
        if value is None:
            if attribute.required:
                errors.append(gettext("%(label)s es obligatorio.") % {"label": attribute.label})
            stored.pop(attribute.key, None)
        else:
            stored[attribute.key] = value
    if errors:
        raise ValidationError(errors)
    changes = audit.diff(asset.attributes, stored)
    if changes:
        asset.attributes = stored
        asset.save(update_fields=["attributes", "updated_at"])
        audit.record(
            action=AuditEvent.Action.UPDATE,
            target=asset,
            membership=membership,
            changes={"attributes": changes},
        )
    return asset
