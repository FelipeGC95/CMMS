import datetime
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.text import slugify
from django.utils.translation import gettext

from apps.accounts.permissions import Perm, PermissionDeniedError, require_perm
from apps.accounts.scoping import allowed_location_ids
from apps.audit import services as audit
from apps.audit.models import AuditEvent
from apps.organizations.models import Location
from apps.organizations.services import check_quota
from apps.webhooks.models import Event as WebhookEvent
from apps.webhooks.services import emit_event

from . import attributes as attribute_values
from .models import (
    MANUAL_STATUSES,
    Asset,
    AssetClass,
    AssetClassAttribute,
    AssetStatusPeriod,
    AttributeType,
    Meter,
    MeterKind,
    MeterReading,
)
from .selectors import visible_assets

# Más de 24 h de uso por día en un horómetro indica un error de digitación.
MAX_HOURS_PER_DAY = Decimal("24")
USAGE_WINDOW_DAYS = 30


class MeterReadingError(ValidationError):
    pass


@transaction.atomic
def change_asset_status(
    *,
    asset: Asset,
    status: str,
    by=None,
    reason: str = "",
    at=None,
    work_order=None,
    failure_event=None,
) -> Asset:
    """Cierra el período de estado vigente y abre uno nuevo (AST-04 CA2).

    `work_order` y `failure_event` enlazan el período con la OT o la falla que
    lo explica. Primitiva sin permisos: la usan los flujos de OT y avisos; los
    cambios manuales pasan por `set_asset_status`.
    """
    if asset.status == status:
        return asset
    at = at or timezone.now()
    AssetStatusPeriod.objects.filter(asset=asset, ended_at__isnull=True).update(ended_at=at)
    AssetStatusPeriod.objects.create(
        asset=asset,
        status=status,
        started_at=at,
        reason=reason[:200],
        changed_by=by,
        work_order=work_order,
        failure_event=failure_event,
    )
    previous = asset.status
    asset.status = status
    asset.save(update_fields=["status", "updated_at"])
    changes = {"status": [previous, status], "reason": reason[:200]}
    if work_order is not None:
        changes["work_order"] = str(work_order.pk)
    if failure_event is not None:
        changes["failure_event"] = str(failure_event.pk)
    audit.record(action=AuditEvent.Action.TRANSITION, target=asset, actor=by, changes=changes)
    emit_event(  # X-08
        event_type=WebhookEvent.ASSET_STATUS_CHANGED, instance=asset, change={"from": previous, "to": status}
    )
    return asset


def _require_manageable_asset(*, membership, asset: Asset) -> None:
    """Permiso `asset.manage` y activo dentro de la organización y del alcance por sitio."""
    require_perm(membership, Perm.ASSET_MANAGE)
    if not visible_assets(membership=membership).filter(pk=asset.pk).exists():
        raise PermissionDeniedError(Perm.ASSET_MANAGE)


def _clean_reason(reason: str) -> str:
    reason = (reason or "").strip()
    if not reason:
        raise ValidationError({"reason": gettext("Indica el motivo del cambio.")})
    return reason[:200]


@transaction.atomic
def set_asset_status(*, membership, asset: Asset, status: str, reason: str) -> Asset:
    """Cambio manual de estado (AST-04): operativo, detenido o en mantención, con motivo."""
    _require_manageable_asset(membership=membership, asset=asset)
    asset = Asset.objects.select_for_update().get(pk=asset.pk)
    if asset.status == Asset.Status.RETIRED:
        raise ValidationError(gettext("El activo está dado de baja: su estado ya no se puede cambiar."))
    if status not in MANUAL_STATUSES:
        raise ValidationError({"status": gettext("Elige un estado válido.")})
    if status == asset.status:
        raise ValidationError({"status": gettext("El activo ya está en ese estado.")})
    return change_asset_status(asset=asset, status=status, by=membership.user, reason=_clean_reason(reason))


@transaction.atomic
def decommission_asset(*, membership, asset: Asset, reason: str) -> Asset:
    """Da de baja el activo (AST-04 CA3): estado "de baja" y planes desactivados.

    No borra nada: el activo, su historial y sus OT se conservan. Las OT
    abiertas no se tocan (la confirmación las muestra como aviso). Sin
    asignaciones activas y en estado de baja, el activo no genera preventivos.
    """
    from apps.maintenance.services import deactivate_asset_assignments

    _require_manageable_asset(membership=membership, asset=asset)
    asset = Asset.objects.select_for_update().get(pk=asset.pk)
    if asset.status == Asset.Status.RETIRED:
        raise ValidationError(gettext("El activo ya está dado de baja."))
    reason = _clean_reason(reason)
    change_asset_status(asset=asset, status=Asset.Status.RETIRED, by=membership.user, reason=reason)
    deactivate_asset_assignments(asset=asset, by=membership.user, reason=reason)
    return asset


@transaction.atomic
def reactivate_asset(*, membership, asset: Asset, reason: str, assignment_ids=()) -> Asset:
    """Reactiva un activo dado de baja (AST-04): cierra el período "de baja" y abre "operativo".

    Los planes desactivados en la baja no vuelven solos: solo se reactivan las
    asignaciones elegidas (`assignment_ids`), con el vencimiento recalculado
    desde hoy. Los componentes no se tocan.
    """
    from apps.maintenance.selectors import reactivatable_assignments_for_asset
    from apps.maintenance.services import reactivate_assignments

    _require_manageable_asset(membership=membership, asset=asset)
    asset = Asset.objects.select_for_update().get(pk=asset.pk)
    if asset.status != Asset.Status.RETIRED:
        raise ValidationError(gettext("El activo no está dado de baja."))
    reason = _clean_reason(reason)
    wanted = {str(pk) for pk in assignment_ids}
    assignments = [a for a in reactivatable_assignments_for_asset(asset=asset) if str(a.pk) in wanted]
    if len(assignments) != len(wanted):
        raise ValidationError(gettext("Elige solo planes desactivados de este activo."))
    change_asset_status(asset=asset, status=Asset.Status.OPERATIONAL, by=membership.user, reason=reason)
    reactivate_assignments(asset=asset, assignments=assignments, by=membership.user)
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

    if meter.is_variable and not meter.is_active:  # desactivada: conserva su historial (CBM-01 CA5)
        raise MeterReadingError(gettext("La variable está desactivada: reactívala para registrar lecturas."))
    value = Decimal(str(value))
    read_at = read_at or timezone.now()
    if value < 0 and not meter.is_variable:  # una temperatura bajo cero es válida (CBM-01)
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
    emit_event(event_type=WebhookEvent.METER_READING_CREATED, instance=reading)  # X-08
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
        return reading, warnings  # lectura histórica: no cambia el valor vigente ni alerta (CBM-04)

    meter.current_value = value
    meter.current_read_at = read_at
    if meter.is_variable:
        # Una variable de condición no tiene uso diario ni dispara preventivos (CBM-01):
        # sus lecturas las evalúan las reglas de condición (CBM-04, ADR-029).
        meter.avg_daily_usage = None
        meter.save(update_fields=["current_value", "current_read_at", "avg_daily_usage", "updated_at"])
        from apps.condition import services as condition

        condition.evaluate_reading(reading=reading, previous_value=previous_value, previous_at=previous_at)
        return reading, warnings
    meter.avg_daily_usage = _average_daily_usage(meter, until=read_at)
    meter.save(update_fields=["current_value", "current_read_at", "avg_daily_usage", "updated_at"])

    from apps.maintenance import services as maintenance

    if is_reset and previous_value is not None:
        maintenance.shift_meter_targets(meter=meter, delta=value - previous_value)
    if generate_work_orders:
        maintenance.generate_due_work_orders(meter=meter)
    return reading, warnings


@transaction.atomic
def create_meter(*, membership, asset: Asset, kind: str, name: str = "", unit: str = "") -> Meter:
    """Agrega un medidor de uso o una variable de condición a un activo (AST-07, CBM-01).

    Una variable de condición siempre es no acumulativa; un medidor de uso, acumulativo.
    La clave en el activo es (tipo, nombre).
    """
    _require_manageable_asset(membership=membership, asset=asset)
    if kind not in MeterKind.values:
        raise ValidationError({"kind": gettext("Elige un tipo de medidor válido.")})
    name, unit = (name or "").strip(), (unit or "").strip()
    if len(name) > 80 or len(unit) > 20:
        raise ValidationError(gettext("Nombre o unidad demasiado largos."))
    _require_unique_meter_key(asset_id=asset.pk, kind=kind, name=name)
    meter = Meter(asset=asset, kind=kind, name=name, unit=unit, is_cumulative=not MeterKind.is_variable(kind))
    meter.save()
    audit.record(
        action=AuditEvent.Action.CREATE,
        target=meter,
        membership=membership,
        changes=audit.snapshot(meter, fields={"asset", "kind", "name", "unit", "is_cumulative"}),
    )
    return meter


def _require_unique_meter_key(*, asset_id, kind: str, name: str, exclude=None) -> None:
    """La clave (tipo, nombre) es única por activo, también contra las variables desactivadas."""
    clash = Meter.objects.filter(asset_id=asset_id, kind=kind, name__iexact=name)
    if exclude is not None:
        clash = clash.exclude(pk=exclude.pk)
    clash = clash.first()
    if clash is None:
        return
    if not clash.is_active:
        message = gettext(
            "El activo ya tiene una variable desactivada de ese tipo con ese nombre: reactívala en vez "
            "de crear otra."
        )
    else:
        message = gettext("El activo ya tiene un medidor de ese tipo con ese nombre.")
    raise ValidationError({"name": message})


def _require_variable(meter: Meter) -> None:
    if not meter.is_variable:
        raise ValidationError(gettext("Solo se editan aquí las variables de condición."))


@transaction.atomic
def create_variable(*, membership, asset: Asset, kind: str, name: str = "", unit: str = "") -> Meter:
    """Alta de una variable de condición desde la ficha del activo (CBM-01 CA1–CA3).

    Solo tipos variable; "Otra variable" exige nombre. Sin unidad, toma la del tipo.
    """
    if not MeterKind.is_variable(kind):
        raise ValidationError({"kind": gettext("Elige un tipo de variable de condición.")})
    if kind == MeterKind.VARIABLE and not (name or "").strip():
        raise ValidationError({"name": gettext("Ponle nombre a la variable (p. ej. Nivel de aceite).")})
    return create_meter(membership=membership, asset=asset, kind=kind, name=name, unit=unit)


@transaction.atomic
def update_variable(*, membership, meter: Meter, name: str, unit: str) -> Meter:
    """Cambia el nombre y la unidad de una variable (CBM-01 CA3). El tipo no cambia.

    Cambiar el nombre cambia la clave (tipo, nombre): las reglas por clase y los ítems de
    checklist que usan el nombre anterior dejan de aplicarle.
    """
    _require_manageable_asset(membership=membership, asset=meter.asset)
    _require_variable(meter)
    name, unit = (name or "").strip(), (unit or "").strip()
    if len(name) > 80 or len(unit) > 20:
        raise ValidationError(gettext("Nombre o unidad demasiado largos."))
    if meter.kind == MeterKind.VARIABLE and not name:
        raise ValidationError({"name": gettext("Ponle nombre a la variable (p. ej. Nivel de aceite).")})
    _require_unique_meter_key(asset_id=meter.asset_id, kind=meter.kind, name=name, exclude=meter)
    before = audit.snapshot(meter, fields={"name", "unit"})
    meter.name, meter.unit = name, unit
    meter.save(update_fields=["name", "unit", "updated_at"])
    changes = audit.diff(before, audit.snapshot(meter, fields={"name", "unit"}))
    if changes:
        audit.record(action=AuditEvent.Action.UPDATE, target=meter, membership=membership, changes=changes)
    return meter


@transaction.atomic
def set_variable_active(*, membership, meter: Meter, is_active: bool) -> Meter:
    """Desactiva o reactiva una variable (CBM-01 CA5): no se borra.

    Desactivada no recibe lecturas ni se ofrece para registrar; su historial, sus estados y
    sus alertas siguen visibles.
    """
    _require_manageable_asset(membership=membership, asset=meter.asset)
    _require_variable(meter)
    meter = Meter.objects.select_for_update().select_related("asset").get(pk=meter.pk)
    if meter.is_active == is_active:
        return meter
    meter.is_active = is_active
    meter.save(update_fields=["is_active", "updated_at"])
    audit.record(
        action=AuditEvent.Action.UPDATE,
        target=meter,
        membership=membership,
        changes={"is_active": [not is_active, is_active]},
    )
    return meter


@transaction.atomic
def ensure_variable(*, asset: Asset, kind: str, name: str = "", unit: str = "", by=None) -> Meter | None:
    """La variable (tipo, nombre) del activo; si no la tiene, la crea con `unit` (CBM-06 CA3).

    Paso del sistema al completar una OT con ítems enlazados: no pide `asset.manage`
    (lo autoriza quien completa la OT). Solo tipos variable (no acumulativos). Devuelve
    None si la variable existe pero está desactivada: no se reactiva sola.
    """
    if not MeterKind.is_variable(kind):
        raise ValidationError({"kind": gettext("Elige un tipo de variable de condición.")})
    name, unit = (name or "").strip()[:80], (unit or "").strip()[:20]
    meter, created = Meter.objects.get_or_create(
        asset=asset, kind=kind, name=name, defaults={"unit": unit, "is_cumulative": False}
    )
    if created:
        audit.record(
            action=AuditEvent.Action.CREATE,
            target=meter,
            actor=by,
            changes={
                **audit.snapshot(meter, fields={"asset", "kind", "name", "unit", "is_cumulative"}),
                "source": "checklist",
            },
        )
    return meter if meter.is_active else None


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
    stored, errors = _merge_attribute_values(asset.asset_class, asset.attributes, values)
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
        emit_event(event_type=WebhookEvent.ASSET_UPDATED, instance=asset, change={"fields": ["attributes"]})
    return asset


def _merge_attribute_values(asset_class, stored: dict, values: dict, *, enforce_required=True):
    """Aplica `values` (por clave) sobre `stored` con los atributos activos de la clase.

    Devuelve (nuevo_dict, errores). Los valores de atributos desactivados o de
    otra clase se conservan (AST-01 CA3).
    """
    stored = dict(stored)
    errors = []
    for attribute in asset_class.attributes.filter(is_active=True):
        try:
            value = attribute_values.to_json(attribute, values.get(attribute.key))
        except ValueError as error:
            errors.append(str(error))
            continue
        if value is None:
            if attribute.required and enforce_required:
                errors.append(gettext("%(label)s es obligatorio.") % {"label": attribute.label})
            stored.pop(attribute.key, None)
        else:
            stored[attribute.key] = value
    return stored, errors


# ---------- Alta y edición de activos (AST-02) y jerarquía (AST-03) ----------

ASSET_FIELDS = [
    "code",
    "name",
    "asset_class",
    "location",
    "parent",
    "criticality",
    "manufacturer",
    "model",
    "serial_number",
    "year",
    "in_service_on",
]
MAX_HIERARCHY_DEPTH = 100


def creates_cycle(*, asset: Asset, parent: Asset) -> bool:
    """True si `parent` es el propio activo o uno de sus descendientes (AST-03 CA1)."""
    node_id, steps = parent.pk, 0
    while node_id is not None and steps < MAX_HIERARCHY_DEPTH:
        if node_id == asset.pk:
            return True
        node_id = Asset.objects.filter(pk=node_id).values_list("parent_id", flat=True).first()
        steps += 1
    return node_id is not None  # jerarquía anormalmente profunda: se trata como ciclo


def _validate_asset(*, membership, asset, code, asset_class, location, parent, criticality, year):
    """Reglas comunes de alta y edición. Lanza ValidationError por campo.

    Toda referencia debe ser de la organización del contexto (el manager
    filtra por ella) y, con alcance por sitio, estar dentro de sus sitios.
    """
    errors = {}
    if not code:
        errors["code"] = gettext("Indica el código.")
    else:
        duplicate = Asset.objects.filter(code__iexact=code)
        if asset is not None:
            duplicate = duplicate.exclude(pk=asset.pk)
        if duplicate.exists():
            errors["code"] = gettext("Ya existe un activo con el código %(code)s.") % {"code": code}

    unchanged_class = asset is not None and asset_class.pk == asset.asset_class_id
    classes = AssetClass.objects.all() if unchanged_class else AssetClass.objects.filter(is_active=True)
    if not classes.filter(pk=asset_class.pk).exists():
        errors["asset_class"] = gettext("Elige una clase de activo válida.")

    if criticality not in Asset.Criticality.values:
        errors["criticality"] = gettext("Criticidad inválida: usa A, B o C.")
    if year is not None and not 1900 <= year <= timezone.localdate().year + 1:
        errors["year"] = gettext("Año inválido.")

    allowed = allowed_location_ids(membership)
    if location is None:
        if allowed is not None:
            errors["location"] = gettext("Indica una ubicación de los sitios a los que tienes acceso.")
    else:
        unchanged_location = asset is not None and location.pk == asset.location_id
        locations = Location.objects.all() if unchanged_location else Location.objects.filter(is_active=True)
        if not locations.filter(pk=location.pk).exists():
            errors["location"] = gettext("Elige una ubicación válida.")
        elif allowed is not None and location.pk not in allowed:
            errors["location"] = gettext("Esa ubicación está fuera de los sitios a los que tienes acceso.")

    if parent is not None:
        unchanged_parent = asset is not None and parent.pk == asset.parent_id
        parents = visible_assets(membership=membership)
        if not unchanged_parent:
            parents = parents.filter(is_active=True)
        if not parents.filter(pk=parent.pk).exists():
            errors["parent"] = gettext("Elige un activo padre válido.")
        elif asset is not None and creates_cycle(asset=asset, parent=parent):
            errors["parent"] = gettext(
                "Un activo no puede depender de sí mismo ni de uno de sus componentes."
            )
    if errors:
        raise ValidationError(errors)


@transaction.atomic
def create_asset(
    *,
    membership,
    code: str,
    name: str,
    asset_class: AssetClass,
    location=None,
    parent: Asset | None = None,
    criticality: str = Asset.Criticality.B,
    manufacturer: str = "",
    model: str = "",
    serial_number: str = "",
    year: int | None = None,
    in_service_on: datetime.date | None = None,
    attributes: dict | None = None,
) -> Asset:
    """Alta de activo (AST-02). Solo código, nombre y clase son obligatorios (CA3).

    Los atributos obligatorios de la clase no bloquean el alta rápida: se
    exigen al editar la ficha.
    """
    require_perm(membership, Perm.ASSET_MANAGE)
    check_quota(organization=membership.organization, resource="assets")  # ORG-10 CA1
    code, name = code.strip(), name.strip()
    _validate_asset(
        membership=membership,
        asset=None,
        code=code,
        asset_class=asset_class,
        location=location,
        parent=parent,
        criticality=criticality,
        year=year,
    )
    stored, errors = _merge_attribute_values(asset_class, {}, attributes or {}, enforce_required=False)
    if errors:
        raise ValidationError(errors)
    asset = Asset.objects.create(
        code=code,
        name=name,
        asset_class=asset_class,
        location=location,
        parent=parent,
        criticality=criticality,
        manufacturer=manufacturer.strip(),
        model=model.strip(),
        serial_number=serial_number.strip(),
        year=year,
        in_service_on=in_service_on,
        attributes=stored,
    )
    # Período inicial: la disponibilidad se mide desde el alta (AST-04).
    AssetStatusPeriod.objects.create(
        asset=asset, status=asset.status, started_at=timezone.now(), changed_by=membership.user
    )
    audit.record(
        action=AuditEvent.Action.CREATE,
        target=asset,
        membership=membership,
        changes={**audit.snapshot(asset, ASSET_FIELDS), "attributes": stored},
    )
    emit_event(event_type=WebhookEvent.ASSET_CREATED, instance=asset)  # X-08
    # Planes por defecto de la clase (PM-04 CA1), igual que en la importación Excel.
    from apps.maintenance.services import assign_class_plans

    assign_class_plans(assets=[asset], by=membership.user)
    return asset


@transaction.atomic
def update_asset(
    *,
    membership,
    asset: Asset,
    code: str,
    name: str,
    asset_class: AssetClass,
    location=None,
    parent: Asset | None = None,
    criticality: str = Asset.Criticality.B,
    manufacturer: str = "",
    model: str = "",
    serial_number: str = "",
    year: int | None = None,
    in_service_on: datetime.date | None = None,
    attributes: dict | None = None,
) -> Asset:
    """Edita la ficha (AST-02). El estado no se edita aquí: cambia con `change_asset_status`.

    `attributes=None` deja los valores de atributos como están.
    """
    require_perm(membership, Perm.ASSET_MANAGE)
    allowed = allowed_location_ids(membership)
    if allowed is not None and asset.location_id not in allowed:
        raise PermissionDeniedError(Perm.ASSET_MANAGE)
    code, name = code.strip(), name.strip()
    _validate_asset(
        membership=membership,
        asset=asset,
        code=code,
        asset_class=asset_class,
        location=location,
        parent=parent,
        criticality=criticality,
        year=year,
    )
    stored = asset.attributes
    if attributes is not None:
        stored, errors = _merge_attribute_values(asset_class, asset.attributes, attributes)
        if errors:
            raise ValidationError(errors)
    before = {**audit.snapshot(asset, ASSET_FIELDS), "attributes": asset.attributes}
    asset.code, asset.name, asset.asset_class = code, name, asset_class
    asset.location, asset.parent, asset.criticality = location, parent, criticality
    asset.manufacturer, asset.model = manufacturer.strip(), model.strip()
    asset.serial_number, asset.year, asset.in_service_on = serial_number.strip(), year, in_service_on
    asset.attributes = stored
    asset.save()
    changes = audit.diff(before, {**audit.snapshot(asset, ASSET_FIELDS), "attributes": stored})
    if changes:
        audit.record(action=AuditEvent.Action.UPDATE, target=asset, membership=membership, changes=changes)
        emit_event(  # X-08
            event_type=WebhookEvent.ASSET_UPDATED, instance=asset, change={"fields": sorted(changes)}
        )
    return asset
