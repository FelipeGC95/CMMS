import datetime
import math
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import F, Q
from django.utils import timezone

from .models import MaintenancePlan, PlanAssignment

DEFAULT_HORIZON_DAYS = 14
# Aviso mínimo para planes por uso: generar la OT al 90 % del intervalo.
METER_MIN_LEAD = Decimal("0.10")


def assign_plan(
    *,
    plan: MaintenancePlan,
    asset,
    starts_on: datetime.date | None = None,
    meter=None,
    next_due_meter_value=None,
) -> PlanAssignment:
    """Aplica un plan a un activo y calcula su primer vencimiento.

    Por calendario, el primer vencimiento es `starts_on` (por defecto, hoy +
    frecuencia). Por uso, es la lectura actual + intervalo.
    """
    defaults = {"is_active": True, "next_due_on": None, "meter": None, "next_due_meter_value": None}
    if plan.interval_days:
        defaults["next_due_on"] = starts_on or timezone.localdate() + datetime.timedelta(
            days=plan.interval_days
        )
    if plan.uses_meter:
        meter = meter or asset.meters.filter(kind=plan.meter_kind, is_active=True).first()
        if meter is None:
            raise ValidationError(
                f"{asset.code} no tiene un medidor de tipo «{plan.get_meter_kind_display()}»."
            )
        defaults["meter"] = meter
        defaults["next_due_meter_value"] = (
            next_due_meter_value
            if next_due_meter_value is not None
            else (meter.current_value or Decimal(0)) + plan.meter_interval
        )
    assignment, _ = PlanAssignment.objects.update_or_create(plan=plan, asset=asset, defaults=defaults)
    return assignment


def _meter_due(assignment: PlanAssignment, horizon_days: int) -> bool:
    meter, target = assignment.meter, assignment.next_due_meter_value
    if meter is None or target is None or meter.current_value is None:
        return False
    lead = max(
        (meter.avg_daily_usage or Decimal(0)) * horizon_days,
        assignment.plan.meter_interval * METER_MIN_LEAD,
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

    Idempotente: una asignación con OT abierta no genera otra (además lo
    garantiza un índice único parcial en WorkOrder). Con `meter`, revisa solo
    los planes de ese medidor (tras registrar una lectura).
    """
    from apps.work.models import WorkOrder
    from apps.work.services import create_work_order

    today = today or timezone.localdate()
    limit = today + datetime.timedelta(days=horizon_days)
    candidates = (
        PlanAssignment.objects.filter(is_active=True, plan__is_active=True, asset__is_active=True)
        .filter(Q(next_due_on__lte=limit) | Q(meter__isnull=False, next_due_meter_value__isnull=False))
        .exclude(asset__status="retired")
        .select_related("plan", "asset", "meter")
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
        by_date = assignment.next_due_on is not None and assignment.next_due_on <= limit
        by_meter = _meter_due(assignment, horizon_days)
        if not (by_date or by_meter):
            continue
        due_dates = [d for d in [assignment.next_due_on] if d is not None]
        if by_meter:
            due_dates.append(_estimated_meter_date(assignment, today))
        plan = assignment.plan
        with transaction.atomic():
            created.append(
                create_work_order(
                    work_type=WorkOrder.Type.PREVENTIVE,
                    title=plan.name,
                    asset=assignment.asset,
                    due_on=min(due_dates),
                    due_meter_value=assignment.next_due_meter_value if assignment.meter_id else None,
                    estimated_minutes=plan.estimated_minutes,
                    plan_assignment=assignment,
                    checklist=list(plan.items.all()),
                    status=WorkOrder.Status.PLANNED,
                )
            )
    return created


def register_completion(
    *, assignment: PlanAssignment, completed_on: datetime.date, meter_value=None
) -> PlanAssignment:
    """Reprograma en modo flotante desde la ejecución real (fecha y lectura)."""
    plan = assignment.plan
    assignment.last_done_on = completed_on
    assignment.next_due_on = (
        completed_on + datetime.timedelta(days=plan.interval_days) if plan.interval_days else None
    )
    if plan.uses_meter and assignment.meter_id:
        base = meter_value if meter_value is not None else assignment.meter.current_value
        if base is not None:
            assignment.last_done_meter_value = base
            assignment.next_due_meter_value = base + plan.meter_interval
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
