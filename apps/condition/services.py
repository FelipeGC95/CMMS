"""Reglas de mantenimiento por condición (CBM-03/04/05, ADR-029, docs/03 §2.11).

Evaluación: `assets.services.record_meter_reading` llama a `evaluate_reading`
después de guardar una lectura que es la más reciente de una variable. Con el
módulo `condition` activo se buscan las reglas que aplican, se bloquean sus
estados (`select_for_update`, en orden) y se aplica la histéresis: solo el paso
de normal a alarma abre un episodio (alerta y, si la regla lo pide, una OT
predictiva). Mientras siga en alarma se actualiza el último valor de la alerta;
al volver a rango la alerta queda `normalized` (la OT sigue su curso). Al
cerrarse o cancelarse la OT vinculada, sus alertas pasan a `closed`
(`close_alerts_for_work_order`, llamada explícita desde `apps.work`).

Si hubo lecturas sin evaluar entre la última evaluada y esta (módulo apagado o
regla inactiva), el estado arranca en normal: el episodio anterior se da por
normalizado y se evalúa de nuevo.

Los estados y el último valor de la alerta se escriben con `update()` (sin
`save()`): evita consultas de verificación de FK en cada lectura. Las
transiciones de las alertas y las reglas quedan en la auditoría.
"""

import datetime
from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext as _

from apps.accounts.permissions import Perm, PermissionDeniedError, require_perm
from apps.assets.models import VARIABLE_KINDS, AssetClass, Meter, MeterKind, MeterReading
from apps.assets.selectors import visible_assets
from apps.audit import services as audit
from apps.audit.models import AuditEvent
from apps.core.tenancy import require_current_organization
from apps.notifications.services import notify_condition_alert
from apps.organizations.modules import ModuleNotIncludedError, has_module
from apps.work.models import WorkOrder

from .models import ConditionAlert, ConditionRule, ConditionState
from .selectors import rules_for_meter

MODULE = "condition"
Operator = ConditionRule.Operator
Action = ConditionRule.Action
AlertStatus = ConditionAlert.Status
StateStatus = ConditionState.Status

MAX_CONSECUTIVE = 100
VALUE_LIMIT = Decimal(10) ** 11  # max_digits=14, decimal_places=3
RULE_FIELDS = (
    "name",
    "operator",
    "threshold",
    "min_value",
    "max_value",
    "consecutive",
    "action",
    "work_order_title",
    "work_order_priority",
    "work_order_instructions",
)
SCOPE_FIELDS = ("meter", "asset_class", "meter_kind", "meter_name")
_UNSET = object()


class ConditionError(ValidationError):
    """Regla de condición inválida o transición de alerta no permitida."""


def module_enabled(organization=None) -> bool:
    return has_module(organization or require_current_organization(), MODULE)


def _require_module() -> None:
    organization = require_current_organization()
    if not has_module(organization, MODULE):
        raise ModuleNotIncludedError(organization, MODULE)


# --- Reglas (CBM-03) ---------------------------------------------------------------


def _decimal(value, field: str) -> Decimal | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    try:
        number = Decimal(str(value).strip().replace(",", "."))
    except (InvalidOperation, ValueError):
        raise ConditionError({field: _("Ingresa un número válido.")}) from None
    if not number.is_finite() or abs(number) >= VALUE_LIMIT:
        raise ConditionError({field: _("Ingresa un número válido.")})
    return number.quantize(Decimal("0.001"))


def _clean_rule_fields(data: dict) -> dict:
    """Valida y normaliza los campos de la condición y de la OT (no el alcance)."""
    errors = {}
    cleaned = {
        "name": (data.get("name") or "").strip(),
        "work_order_title": (data.get("work_order_title") or "").strip(),
        "work_order_instructions": (data.get("work_order_instructions") or "").strip(),
    }
    if len(cleaned["name"]) > 120:
        errors["name"] = _("Máximo %(n)s caracteres.") % {"n": 120}
    if len(cleaned["work_order_title"]) > 200:
        errors["work_order_title"] = _("Máximo %(n)s caracteres.") % {"n": 200}

    operator = data.get("operator")
    if operator not in Operator.values:
        errors["operator"] = _("Elige una condición válida.")
    cleaned["operator"] = operator
    threshold = _decimal(data.get("threshold"), "threshold")
    min_value = _decimal(data.get("min_value"), "min_value")
    max_value = _decimal(data.get("max_value"), "max_value")
    if operator == Operator.OUTSIDE:
        threshold = None
        if min_value is None or max_value is None:
            errors["min_value"] = _("Indica el mínimo y el máximo del rango.")
        elif min_value >= max_value:
            errors["max_value"] = _("El máximo debe ser mayor que el mínimo.")
    elif operator in Operator.values:
        min_value = max_value = None
        if threshold is None:
            errors["threshold"] = _("Indica el umbral.")
        elif operator == Operator.RATE and threshold <= 0:
            errors["threshold"] = _("El cambio por día debe ser mayor que cero.")
    cleaned.update(threshold=threshold, min_value=min_value, max_value=max_value)

    try:
        raw = data.get("consecutive")
        consecutive = 1 if raw in (None, "") else int(raw)
    except (TypeError, ValueError):
        consecutive = 0
    if not 1 <= consecutive <= MAX_CONSECUTIVE:
        errors["consecutive"] = _("Indica un número entre 1 y %(max)s.") % {"max": MAX_CONSECUTIVE}
    cleaned["consecutive"] = consecutive

    action = data.get("action") or Action.ALERT_WORK_ORDER
    if action not in Action.values:
        errors["action"] = _("Elige una acción válida.")
    cleaned["action"] = action
    priority = data.get("work_order_priority")
    if priority in ("", None):
        priority = None
    else:
        try:
            priority = int(priority)
        except (TypeError, ValueError):
            priority = 0
        if priority not in ConditionRule.Priority.values:
            errors["work_order_priority"] = _("Elige una prioridad válida.")
    cleaned["work_order_priority"] = priority
    if errors:
        raise ConditionError(errors)
    return cleaned


def _clean_scope(*, membership, meter, asset_class, meter_kind, meter_name) -> dict:
    """Alcance exclusivo: un medidor visible de tipo variable, o una clase + tipo variable + nombre."""
    meter_name = (meter_name or "").strip()
    if (meter is None) == (asset_class is None):
        raise ConditionError(_("Elige una variable de un activo o una clase de activo, no ambas."))
    if meter is not None:
        # El medidor debe ser de esta organización y de un activo en el alcance (ORG-06).
        if not Meter.objects.filter(pk=meter.pk, asset__in=visible_assets(membership=membership)).exists():
            raise ConditionError({"meter": _("Variable no encontrada.")})
        if not MeterKind.is_variable(meter.kind):
            raise ConditionError(
                {"meter": _("Las reglas de condición se definen sobre variables, no sobre medidores de uso.")}
            )
        return {"meter": meter, "asset_class": None, "meter_kind": "", "meter_name": ""}
    if not AssetClass.objects.filter(pk=asset_class.pk).exists():
        raise ConditionError({"asset_class": _("Clase de activo no encontrada.")})
    if meter_kind not in VARIABLE_KINDS:
        raise ConditionError({"meter_kind": _("Elige un tipo de variable.")})
    if len(meter_name) > 80:
        raise ConditionError({"meter_name": _("Máximo %(n)s caracteres.") % {"n": 80}})
    return {"meter": None, "asset_class": asset_class, "meter_kind": meter_kind, "meter_name": meter_name}


def _require_rule_manageable(*, membership, rule: ConditionRule) -> None:
    require_perm(membership, Perm.CONDITION_MANAGE)
    if rule.meter_id and not visible_assets(membership=membership).filter(meters=rule.meter_id).exists():
        raise PermissionDeniedError(Perm.CONDITION_MANAGE)


@transaction.atomic
def create_rule(
    *,
    membership,
    operator: str,
    meter: Meter | None = None,
    asset_class: AssetClass | None = None,
    meter_kind: str = "",
    meter_name: str = "",
    threshold=None,
    min_value=None,
    max_value=None,
    consecutive: int = 1,
    action: str = Action.ALERT_WORK_ORDER,
    name: str = "",
    work_order_title: str = "",
    work_order_priority: int | None = None,
    work_order_instructions: str = "",
) -> ConditionRule:
    """Crea una regla sobre una variable o sobre una clase de activo (CBM-03)."""
    require_perm(membership, Perm.CONDITION_MANAGE)
    _require_module()
    scope = _clean_scope(
        membership=membership,
        meter=meter,
        asset_class=asset_class,
        meter_kind=meter_kind,
        meter_name=meter_name,
    )
    fields = _clean_rule_fields(
        {
            "name": name,
            "operator": operator,
            "threshold": threshold,
            "min_value": min_value,
            "max_value": max_value,
            "consecutive": consecutive,
            "action": action,
            "work_order_title": work_order_title,
            "work_order_priority": work_order_priority,
            "work_order_instructions": work_order_instructions,
        }
    )
    rule = ConditionRule(**scope, **fields, created_by=membership.user)
    rule.save()
    audit.record(
        action=AuditEvent.Action.CREATE,
        target=rule,
        membership=membership,
        changes=audit.snapshot(rule, fields={*SCOPE_FIELDS, *RULE_FIELDS, "is_active"}),
    )
    return rule


@transaction.atomic
def update_rule(*, membership, rule: ConditionRule, **values) -> ConditionRule:
    """Cambia la condición o los datos de la OT. El alcance no cambia (se crea otra regla).

    `values`: cualquiera de RULE_FIELDS; los omitidos conservan su valor. La próxima
    lectura se evalúa con la condición nueva; el episodio en curso sigue.
    """
    _require_rule_manageable(membership=membership, rule=rule)
    _require_module()
    unknown = set(values) - set(RULE_FIELDS)
    if unknown:
        raise ConditionError(_("El alcance de una regla no se cambia: crea otra regla."))
    rule = ConditionRule.objects.select_for_update().get(pk=rule.pk)
    before = audit.snapshot(rule, fields=set(RULE_FIELDS))
    fields = _clean_rule_fields({name: values.get(name, getattr(rule, name)) for name in RULE_FIELDS})
    for name, value in fields.items():
        setattr(rule, name, value)
    rule.save()
    changes = audit.diff(before, audit.snapshot(rule, fields=set(RULE_FIELDS)))
    if changes:
        audit.record(action=AuditEvent.Action.UPDATE, target=rule, membership=membership, changes=changes)
    return rule


@transaction.atomic
def set_rule_active(*, membership, rule: ConditionRule, is_active: bool) -> ConditionRule:
    """Activa o desactiva una regla (sin borrado físico).

    Al desactivarla, sus episodios abiertos quedan normalizados y sus estados vuelven a
    normal: si se reactiva, arranca de cero.
    """
    _require_rule_manageable(membership=membership, rule=rule)
    _require_module()
    rule = ConditionRule.objects.select_for_update().get(pk=rule.pk)
    if rule.is_active == is_active:
        return rule
    rule.is_active = is_active
    rule.save(update_fields=["is_active", "updated_at"])
    audit.record(
        action=AuditEvent.Action.UPDATE,
        target=rule,
        membership=membership,
        changes={"is_active": [not is_active, is_active]},
    )
    if not is_active:
        now = timezone.now()
        for alert in ConditionAlert.objects.filter(rule=rule, status__in=ConditionAlert.ACTIVE_STATUSES):
            _normalize(alert, at=now, actor=membership.user, reason="rule_deactivated")
        ConditionState.objects.filter(rule=rule).update(
            status=StateStatus.NORMAL, consecutive_count=0, open_alert=None, updated_at=now
        )
    return rule


# --- Alertas (CBM-05) ----------------------------------------------------------------


def _transition(alert: ConditionAlert, *, to_status: str, actor=None, extra=None, **fields) -> None:
    from_status = alert.status
    now = timezone.now()
    ConditionAlert.objects.filter(pk=alert.pk).update(status=to_status, updated_at=now, **fields)
    alert.status = to_status
    for name, value in fields.items():
        setattr(alert, name, value)
    audit.record(
        action=AuditEvent.Action.TRANSITION,
        target=alert,
        actor=actor,
        changes={"status": [from_status, to_status], **(extra or {})},
    )


def _normalize(alert: ConditionAlert, *, at, actor=None, reason="", value=None) -> None:
    fields = {"normalized_at": at}
    extra = {"reason": reason} if reason else {}
    if value is not None:
        fields.update(last_value=value, last_read_at=at)
        extra["value"] = value
    _transition(alert, to_status=AlertStatus.NORMALIZED, actor=actor, extra=extra, **fields)


@transaction.atomic
def acknowledge_alert(*, membership, alert: ConditionAlert) -> ConditionAlert:
    """Alguien vio la alerta abierta y se hace cargo (reconocer otra vez no hace nada)."""
    require_perm(membership, Perm.CONDITION_MANAGE)
    _require_module()
    if not visible_assets(membership=membership).filter(pk=alert.asset_id).exists():
        raise PermissionDeniedError(Perm.CONDITION_MANAGE)
    alert = ConditionAlert.objects.select_for_update().get(pk=alert.pk)
    if alert.status == AlertStatus.ACKNOWLEDGED:
        return alert
    if alert.status != AlertStatus.OPEN:
        raise ConditionError(_("Solo se reconoce una alerta abierta."))
    _transition(
        alert,
        to_status=AlertStatus.ACKNOWLEDGED,
        actor=membership.user,
        acknowledged_by=membership.user,
        acknowledged_at=timezone.now(),
    )
    return alert


def close_alerts_for_work_order(*, work_order, by=None) -> int:
    """La OT vinculada se cerró o se canceló: sus alertas pendientes pasan a `closed`.

    Llamada explícita desde `apps.work.services` (cierre y cancelación), sin signals.
    """
    alerts = list(
        ConditionAlert.objects.filter(work_order=work_order, status__in=ConditionAlert.PENDING_STATUSES)
    )
    now = timezone.now()
    for alert in alerts:
        _transition(
            alert,
            to_status=AlertStatus.CLOSED,
            actor=by,
            extra={"work_order": str(work_order.pk), "work_order_status": work_order.status},
            closed_at=now,
        )
    return len(alerts)


# --- Evaluación (CBM-04) -------------------------------------------------------------


def breaches(
    rule: ConditionRule, value: Decimal, *, previous_value=None, previous_at=None, read_at=None
) -> bool:
    """¿La lectura está fuera de rango según la regla? En `rate`, sin lectura anterior no evalúa."""
    if rule.operator == Operator.GT:
        return value > rule.threshold
    if rule.operator == Operator.LT:
        return value < rule.threshold
    if rule.operator == Operator.OUTSIDE:
        return value < rule.min_value or value > rule.max_value
    if previous_value is None or previous_at is None or read_at is None:
        return False
    days = Decimal((read_at - previous_at).total_seconds()) / 86400
    if days <= 0:
        return False
    return abs(value - previous_value) / days > rule.threshold


def _previous_reading(reading: MeterReading) -> tuple[Decimal | None, datetime.datetime | None]:
    row = (
        MeterReading.objects.filter(meter_id=reading.meter_id, read_at__lte=reading.read_at)
        .exclude(pk=reading.pk)
        .order_by("-read_at", "-created_at")
        .values_list("value", "read_at")
        .first()
    )
    return row if row else (None, None)


def _locked_states(meter: Meter, rules: list) -> dict:
    """Estados de (regla, medidor) bloqueados en orden; crea los que falten (arrancan en normal)."""
    rule_ids = [rule.pk for rule in rules]
    states = {
        state.rule_id: state
        for state in ConditionState.objects.select_for_update()
        .filter(meter=meter, rule_id__in=rule_ids)
        .order_by("rule_id")
    }
    missing = [rule_id for rule_id in rule_ids if rule_id not in states]
    if missing:
        organization = require_current_organization()
        ConditionState.objects.bulk_create(
            [ConditionState(organization=organization, rule_id=rule_id, meter=meter) for rule_id in missing],
            ignore_conflicts=True,
        )
        states.update(
            {
                state.rule_id: state
                for state in ConditionState.objects.select_for_update()
                .filter(meter=meter, rule_id__in=missing)
                .order_by("rule_id")
            }
        )
    return states


def evaluate_reading(
    *, reading: MeterReading, previous_value=_UNSET, previous_at=_UNSET
) -> list[ConditionAlert]:
    """Evalúa las reglas de la variable con su lectura más reciente (CBM-04).

    `previous_value`/`previous_at`: lectura vigente antes de esta (la entrega
    `record_meter_reading`); si no se pasan, se consultan. Devuelve las alertas abiertas.
    Idempotente: una lectura ya evaluada, o anterior a la última evaluada, no hace nada.
    """
    meter = reading.meter
    if not MeterKind.is_variable(meter.kind) or not module_enabled():
        return []
    rules = list(rules_for_meter(meter))
    if not rules:
        return []
    if previous_value is _UNSET or previous_at is _UNSET:
        previous_value, previous_at = _previous_reading(reading)
    with transaction.atomic():
        states = _locked_states(meter, rules)
        opened = []
        for rule in rules:
            alert = _evaluate_rule(
                rule, states[rule.pk], reading=reading, previous_value=previous_value, previous_at=previous_at
            )
            if alert is not None:
                opened.append(alert)
    return opened


def _evaluate_rule(rule, state, *, reading, previous_value, previous_at) -> ConditionAlert | None:
    if state.last_reading_id == reading.pk:
        return None  # ya evaluada
    if state.last_evaluated_at is not None and reading.read_at < state.last_evaluated_at:
        return None  # llegó después otra más reciente
    open_alert_id = state.open_alert_id
    in_alarm = state.status == StateStatus.ALARM
    count = state.consecutive_count
    # Lecturas sin evaluar entre la última evaluada y esta (módulo apagado, regla inactiva):
    # el estado arranca en normal.
    gap = (
        state.last_evaluated_at is not None
        and previous_at is not None
        and previous_at != state.last_evaluated_at
    )
    if gap:
        if in_alarm and open_alert_id:
            _normalize_episode(open_alert_id, at=reading.read_at, reason="resumed")
        in_alarm, count, open_alert_id = False, 0, None

    value = reading.value
    opened = None
    if breaches(rule, value, previous_value=previous_value, previous_at=previous_at, read_at=reading.read_at):
        count = min(count + 1, 32767)
        if not in_alarm and count >= rule.consecutive:
            opened = _open_episode(rule, reading=reading)
            in_alarm, open_alert_id = True, opened.pk
        elif in_alarm and open_alert_id:
            ConditionAlert.objects.filter(pk=open_alert_id, status__in=ConditionAlert.ACTIVE_STATUSES).update(
                last_value=value, last_read_at=reading.read_at, updated_at=timezone.now()
            )
    else:
        count = 0
        if in_alarm:
            if open_alert_id:
                _normalize_episode(open_alert_id, at=reading.read_at, value=value)
            in_alarm, open_alert_id = False, None

    ConditionState.objects.filter(pk=state.pk).update(
        status=StateStatus.ALARM if in_alarm else StateStatus.NORMAL,
        consecutive_count=count,
        last_reading=reading,
        last_value=value,
        last_evaluated_at=reading.read_at,
        open_alert_id=open_alert_id,
        updated_at=timezone.now(),
    )
    return opened


def _normalize_episode(alert_id, *, at, value=None, reason="") -> None:
    alert = (
        ConditionAlert.objects.select_for_update()
        .filter(pk=alert_id, status__in=ConditionAlert.ACTIVE_STATUSES)
        .first()
    )
    if alert is not None:  # cerrada por su OT: queda cerrada
        _normalize(alert, at=at, value=value, reason=reason)


def _open_episode(rule: ConditionRule, *, reading: MeterReading) -> ConditionAlert:
    """Normal → alarma: alerta, OT predictiva (o la abierta del episodio anterior) y aviso."""
    meter = reading.meter
    asset = meter.asset
    work_order = None
    if rule.action == Action.ALERT_WORK_ORDER:
        work_order = _open_work_order(rule, meter=meter, asset=asset, reading=reading)
    alert = ConditionAlert(
        rule=rule,
        meter=meter,
        asset=asset,
        reading=reading,
        value=reading.value,
        opened_at=reading.read_at,
        last_value=reading.value,
        last_read_at=reading.read_at,
        work_order=work_order,
    )
    alert.save()
    audit.record(
        action=AuditEvent.Action.CREATE,
        target=alert,
        changes={
            "rule": str(rule.pk),
            "meter": str(meter.pk),
            "value": reading.value,
            "condition": rule.condition_label,
            "work_order": str(work_order.pk) if work_order else None,
        },
    )
    notify_condition_alert(alert=alert)
    return alert


def _open_work_order(rule, *, meter, asset, reading) -> WorkOrder:
    """La OT abierta de un episodio anterior de esta regla y variable, o una predictiva nueva."""
    from apps.work import services as work  # apps.work usa este módulo: import diferido

    existing = (
        WorkOrder.objects.filter(condition_alerts__rule=rule, condition_alerts__meter=meter, asset=asset)
        .exclude(status__in=WorkOrder.CLOSED_STATUSES)
        .order_by("-created_at")
        .first()
    )
    if existing is not None:
        return existing
    from apps.maintenance.models import format_number

    variable = meter.name or meter.get_kind_display()
    title = rule.work_order_title or _("Condición: %(variable)s en %(asset)s") % {
        "variable": variable,
        "asset": asset.code,
    }
    detail = _("Lectura de %(value)s %(unit)s el %(date)s fuera de rango (%(condition)s).") % {
        "value": format_number(reading.value),
        "unit": meter.unit,
        "date": timezone.localtime(reading.read_at).strftime("%d-%m-%Y %H:%M"),
        "condition": rule.condition_label,
    }
    description = "\n\n".join(part for part in (rule.work_order_instructions, detail) if part)
    priority = rule.work_order_priority or work._suggest_priority(asset, asset.status == asset.Status.STOPPED)
    return work.create_work_order(
        work_type=WorkOrder.Type.PREDICTIVE,
        title=title,
        asset=asset,
        description=description,
        priority=priority,
        status=WorkOrder.Status.PLANNED,
        by=None,
    )
