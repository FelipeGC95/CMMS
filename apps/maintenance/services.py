"""Planes preventivos: versiones, asignaciones y programador (PM-01..05, ADR-016)."""

import datetime
import math
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import F, Max, Q
from django.utils import timezone

from .models import MaintenancePlan, PlanAssignment, PlanResource, PlanTaskItem, PlanVersion

DEFAULT_HORIZON_DAYS = 14
# Aviso mínimo para planes por uso: generar la OT al 90 % del intervalo.
METER_MIN_LEAD = Decimal("0.10")
# Campos de la versión que cambian cuándo vence un plan.
TRIGGER_FIELDS = ("interval_days", "meter_kind", "meter_interval")
ITEM_FIELDS = ("order", "text", "item_type", "unit", "min_value", "max_value", "required")
RESOURCE_FIELDS = ("kind", "description", "quantity", "unit")


class PlanVersionError(ValidationError):
    pass


# --- Versiones --------------------------------------------------------------


def locked_plans():
    """Planes para bloquear con SELECT … FOR UPDATE.

    El manager por defecto hace select_related("current_version"), un LEFT JOIN
    porque la FK es nullable, y PostgreSQL no admite FOR UPDATE sobre el lado
    nullable de un outer join. Por eso se quita el join antes de bloquear
    (SQLite no bloquea filas y no reproduce el error).
    """
    return MaintenancePlan.objects.select_related(None).select_for_update()


def _add_content(version: PlanVersion, *, items=(), resources=()):
    PlanTaskItem.objects.bulk_create(
        [
            PlanTaskItem(organization=version.organization, version=version, **{"order": i, **item})
            for i, item in enumerate(items, start=1)
        ]
    )
    PlanResource.objects.bulk_create(
        [PlanResource(organization=version.organization, version=version, **res) for res in resources]
    )


@transaction.atomic
def create_plan(
    *,
    code: str,
    name: str,
    asset_class=None,
    items=(),
    resources=(),
    by=None,
    **version_fields,
) -> MaintenancePlan:
    """Crea un plan con su versión 1 ya publicada (el camino corto para planes nuevos).

    `items`: dicts con text, item_type y opcionalmente unit, min_value,
    max_value, required. `version_fields`: interval_days, meter_kind,
    meter_interval, estimated_minutes, instructions, schedule_mode, tolerance_pct.
    """
    plan = MaintenancePlan.objects.create(code=code, name=name, asset_class=asset_class)
    version = PlanVersion.objects.create(plan=plan, number=1, **version_fields)
    _add_content(version, items=items, resources=resources)
    publish_version(version=version, by=by, change_note="Versión inicial")
    plan.refresh_from_db()
    return plan


@transaction.atomic
def start_draft(*, plan: MaintenancePlan) -> PlanVersion:
    """Devuelve el borrador del plan; si no hay, lo crea copiando la versión vigente."""
    draft = plan.versions.filter(status=PlanVersion.Status.DRAFT).first()
    if draft:
        return draft
    source = plan.current_version
    number = (plan.versions.aggregate(n=Max("number"))["n"] or 0) + 1
    copied = {}
    if source is not None:
        copied = {f: getattr(source, f) for f in PlanVersion.CONTENT_FIELDS if f not in ("plan_id", "number")}
    draft = PlanVersion.objects.create(plan=plan, number=number, **copied)
    if source is not None:
        _add_content(
            draft,
            items=[{f: getattr(i, f) for f in ITEM_FIELDS if f != "order"} for i in source.items.all()],
            resources=[{f: getattr(r, f) for f in RESOURCE_FIELDS} for r in source.resources.all()],
        )
    return draft


@transaction.atomic
def publish_version(*, version: PlanVersion, by=None, change_note: str = "") -> PlanVersion:
    """Publica un borrador: pasa a ser la versión vigente y la anterior queda reemplazada.

    Si cambió la frecuencia, recalcula en la misma transacción los vencimientos
    de las asignaciones sin OT abierta (las OT futuras usan la nueva versión).
    """
    version = PlanVersion.objects.select_for_update().get(pk=version.pk)
    if not version.is_draft:
        raise PlanVersionError(f"La versión {version.number} ya está publicada.")
    version.clean()
    if not version.items.exists():
        raise PlanVersionError("Agrega al menos un ítem al checklist antes de publicar.")

    plan = locked_plans().get(pk=version.plan_id)
    previous = plan.current_version
    if previous is not None:
        previous.status = PlanVersion.Status.SUPERSEDED
        previous.save(update_fields=["status", "updated_at"])
    version.status = PlanVersion.Status.PUBLISHED
    version.published_at = timezone.now()
    version.published_by = by
    version.change_note = change_note[:300] or version.change_note
    version.save(update_fields=["status", "published_at", "published_by", "change_note", "updated_at"])
    plan.current_version = version
    plan.save(update_fields=["current_version", "updated_at"])

    if previous is not None and any(getattr(previous, f) != getattr(version, f) for f in TRIGGER_FIELDS):
        reschedule_after_trigger_change(plan=plan, previous=previous, current=version)
    return version


def reschedule_after_trigger_change(*, plan, previous: PlanVersion, current: PlanVersion) -> int:
    """Recalcula vencimientos con la nueva frecuencia desde la última ejecución (o lectura).

    Las asignaciones con una OT abierta no se tocan: esa OT ya está en curso y
    al cerrarla se reprograma con la versión vigente.
    """
    from apps.work.models import WorkOrder

    open_ids = set(
        WorkOrder.objects.filter(plan_assignment__plan=plan)
        .exclude(status__in=WorkOrder.CLOSED_STATUSES)
        .values_list("plan_assignment_id", flat=True)
    )
    today = timezone.localdate()
    changed = 0
    for assignment in plan.assignments.filter(is_active=True).select_related("asset", "meter"):
        if assignment.id in open_ids:
            continue
        # Calendario: desde la última ejecución; si nunca se ejecutó, desde el
        # inicio implícito del ciclo anterior (vencimiento − frecuencia anterior).
        base_date = assignment.last_done_on
        if base_date is None and assignment.next_due_on and previous.interval_days:
            base_date = assignment.next_due_on - datetime.timedelta(days=previous.interval_days)
        assignment.next_due_on = (
            (base_date or today) + datetime.timedelta(days=current.interval_days)
            if current.interval_days
            else None
        )
        # Uso: igual, con la última lectura de cierre o el inicio del ciclo anterior.
        if current.uses_meter:
            meter = assignment.meter
            if meter is None or meter.kind != current.meter_kind:
                meter = assignment.asset.meters.filter(kind=current.meter_kind, is_active=True).first()
            base_value = assignment.last_done_meter_value
            if base_value is None and assignment.next_due_meter_value is not None and previous.uses_meter:
                base_value = assignment.next_due_meter_value - previous.meter_interval
            if base_value is None and meter is not None:
                base_value = meter.current_value or Decimal(0)
            assignment.meter = meter
            assignment.next_due_meter_value = (
                base_value + current.meter_interval if meter is not None and base_value is not None else None
            )
        else:
            assignment.meter, assignment.next_due_meter_value = None, None
        assignment.save(update_fields=["next_due_on", "meter", "next_due_meter_value", "updated_at"])
        changed += 1
    return changed


# --- Asignaciones -----------------------------------------------------------


def assign_plan(
    *,
    plan: MaintenancePlan,
    asset,
    starts_on: datetime.date | None = None,
    meter=None,
    next_due_meter_value=None,
) -> PlanAssignment:
    """Aplica un plan a un activo y calcula su primer vencimiento con la versión vigente.

    Por calendario, el primer vencimiento es `starts_on` (por defecto, hoy +
    frecuencia). Por uso, es la lectura actual + intervalo.
    """
    version = plan.current_version
    if version is None:
        raise PlanVersionError(f"«{plan}» no tiene una versión publicada.")
    defaults = {"is_active": True, "next_due_on": None, "meter": None, "next_due_meter_value": None}
    if version.interval_days:
        defaults["next_due_on"] = starts_on or timezone.localdate() + datetime.timedelta(
            days=version.interval_days
        )
    if version.uses_meter:
        meter = meter or asset.meters.filter(kind=version.meter_kind, is_active=True).first()
        if meter is None:
            raise ValidationError(
                f"{asset.code} no tiene un medidor de tipo «{version.get_meter_kind_display()}»."
            )
        defaults["meter"] = meter
        defaults["next_due_meter_value"] = (
            next_due_meter_value
            if next_due_meter_value is not None
            else (meter.current_value or Decimal(0)) + version.meter_interval
        )
    assignment, _ = PlanAssignment.objects.update_or_create(plan=plan, asset=asset, defaults=defaults)
    return assignment


# --- Programador ------------------------------------------------------------


def _meter_due(assignment: PlanAssignment, version: PlanVersion, horizon_days: int) -> bool:
    meter, target = assignment.meter, assignment.next_due_meter_value
    if meter is None or target is None or meter.current_value is None or not version.uses_meter:
        return False
    lead = max(
        (meter.avg_daily_usage or Decimal(0)) * horizon_days,
        version.meter_interval * METER_MIN_LEAD,
    )
    return meter.current_value + lead >= target


def _estimated_meter_date(assignment: PlanAssignment, today: datetime.date) -> datetime.date:
    """Fecha estimada en que el medidor llegará al vencimiento (según uso promedio)."""
    meter = assignment.meter
    remaining = assignment.next_due_meter_value - meter.current_value
    if remaining <= 0 or not meter.avg_daily_usage:
        return today
    return today + datetime.timedelta(days=math.ceil(remaining / meter.avg_daily_usage))


def generate_due_work_orders(
    *, today: datetime.date | None = None, horizon_days=DEFAULT_HORIZON_DAYS, meter=None
):
    """Genera OT preventivas que vencen por fecha o por uso, lo que ocurra primero (PM-02, PM-05).

    Usa la versión vigente del plan: la OT guarda esa versión y copia su
    checklist. Idempotente: una asignación con OT abierta no genera otra
    (además lo garantiza un índice único parcial en WorkOrder). Con `meter`,
    revisa solo los planes de ese medidor (tras registrar una lectura).
    """
    from apps.work.models import WorkOrder
    from apps.work.services import create_work_order

    today = today or timezone.localdate()
    limit = today + datetime.timedelta(days=horizon_days)
    candidates = (
        PlanAssignment.objects.filter(
            is_active=True,
            plan__is_active=True,
            plan__current_version__isnull=False,
            asset__is_active=True,
        )
        .filter(Q(next_due_on__lte=limit) | Q(meter__isnull=False, next_due_meter_value__isnull=False))
        .exclude(asset__status="retired")
        .select_related("plan__current_version", "asset", "meter")
    )
    if meter is not None:
        candidates = candidates.filter(meter=meter)
    open_ids = set(
        WorkOrder.objects.filter(plan_assignment__in=candidates)
        .exclude(status__in=WorkOrder.CLOSED_STATUSES)
        .values_list("plan_assignment_id", flat=True)
    )
    created = []
    for assignment in candidates:
        if assignment.id in open_ids:
            continue
        version = assignment.plan.current_version
        by_date = assignment.next_due_on is not None and assignment.next_due_on <= limit
        by_meter = _meter_due(assignment, version, horizon_days)
        if not (by_date or by_meter):
            continue
        due_dates = [d for d in [assignment.next_due_on] if d is not None]
        if by_meter:
            due_dates.append(_estimated_meter_date(assignment, today))
        with transaction.atomic():
            created.append(
                create_work_order(
                    work_type=WorkOrder.Type.PREVENTIVE,
                    title=assignment.plan.name,
                    asset=assignment.asset,
                    due_on=min(due_dates),
                    due_meter_value=assignment.next_due_meter_value if assignment.meter_id else None,
                    estimated_minutes=version.estimated_minutes,
                    plan_assignment=assignment,
                    plan_version=version,
                    checklist=list(version.items.all()),
                    status=WorkOrder.Status.PLANNED,
                )
            )
    return created


def _next_fixed_target(previous, interval, after):
    """Modo fijo: siguiente objetivo = anterior + k·intervalo, el primero posterior a `after`.

    Así, una OT cerrada con más de un intervalo de atraso no genera OT en el pasado.
    """
    target = previous + interval
    if target <= after:
        periods = math.floor((after - previous) / interval) + 1
        target = previous + interval * periods
    return target


def register_completion(
    *, assignment: PlanAssignment, completed_on: datetime.date, meter_value=None
) -> PlanAssignment:
    """Reprograma al cerrar la OT con la versión vigente del plan.

    Flotante: desde la ejecución real (fecha y lectura). Fijo: desde lo
    programado, sin quedar en el pasado.
    """
    version = assignment.plan.current_version
    fixed = version.schedule_mode == PlanVersion.ScheduleMode.FIXED
    assignment.last_done_on = completed_on
    if version.interval_days:
        interval = datetime.timedelta(days=version.interval_days)
        if fixed and assignment.next_due_on is not None:
            assignment.next_due_on = _next_fixed_target(assignment.next_due_on, interval, completed_on)
        else:
            assignment.next_due_on = completed_on + interval
    else:
        assignment.next_due_on = None
    if version.uses_meter and assignment.meter_id:
        base = meter_value if meter_value is not None else assignment.meter.current_value
        if base is not None:
            assignment.last_done_meter_value = base
            if fixed and assignment.next_due_meter_value is not None:
                assignment.next_due_meter_value = _next_fixed_target(
                    assignment.next_due_meter_value, version.meter_interval, base
                )
            else:
                assignment.next_due_meter_value = base + version.meter_interval
    assignment.save(
        update_fields=[
            "last_done_on",
            "next_due_on",
            "last_done_meter_value",
            "next_due_meter_value",
            "updated_at",
        ]
    )
    return assignment


def shift_meter_targets(*, meter, delta: Decimal) -> None:
    """Tras reiniciar o cambiar un medidor, conserva lo que faltaba para cada vencimiento."""
    PlanAssignment.objects.filter(meter=meter, next_due_meter_value__isnull=False).update(
        next_due_meter_value=F("next_due_meter_value") + delta
    )
    PlanAssignment.objects.filter(meter=meter, last_done_meter_value__isnull=False).update(
        last_done_meter_value=F("last_done_meter_value") + delta
    )
