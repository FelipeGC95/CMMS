import datetime

from django.db import transaction
from django.utils import timezone

from .models import MaintenancePlan, PlanAssignment

DEFAULT_HORIZON_DAYS = 14


def assign_plan(*, plan: MaintenancePlan, asset, starts_on: datetime.date) -> PlanAssignment:
    assignment, _ = PlanAssignment.objects.update_or_create(
        plan=plan, asset=asset, defaults={"next_due_on": starts_on, "is_active": True}
    )
    return assignment


def generate_due_work_orders(*, today: datetime.date | None = None, horizon_days=DEFAULT_HORIZON_DAYS):
    """Genera OT preventivas para vencimientos dentro del horizonte (PM-05).

    Idempotente: una asignación con OT abierta no genera otra (además lo
    garantiza un índice único parcial en WorkOrder).
    """
    from apps.work.models import WorkOrder
    from apps.work.services import create_work_order

    today = today or timezone.localdate()
    limit = today + datetime.timedelta(days=horizon_days)
    due = (
        PlanAssignment.objects.filter(
            is_active=True, plan__is_active=True, asset__is_active=True, next_due_on__lte=limit
        )
        .exclude(asset__status="retired")
        .select_related("plan", "asset")
    )
    open_ids = set(
        WorkOrder.objects.filter(plan_assignment__in=due)
        .exclude(status__in=WorkOrder.CLOSED_STATUSES)
        .values_list("plan_assignment_id", flat=True)
    )
    created = []
    for assignment in due:
        if assignment.id in open_ids:
            continue
        plan = assignment.plan
        with transaction.atomic():
            created.append(
                create_work_order(
                    work_type=WorkOrder.Type.PREVENTIVE,
                    title=plan.name,
                    asset=assignment.asset,
                    due_on=assignment.next_due_on,
                    estimated_minutes=plan.estimated_minutes,
                    plan_assignment=assignment,
                    checklist=list(plan.items.all()),
                    status=WorkOrder.Status.PLANNED,
                )
            )
    return created


def register_completion(*, assignment: PlanAssignment, completed_on: datetime.date) -> PlanAssignment:
    """Reprograma en modo flotante: próximo = ejecución real + frecuencia."""
    assignment.last_done_on = completed_on
    assignment.next_due_on = completed_on + datetime.timedelta(days=assignment.plan.interval_days)
    assignment.save(update_fields=["last_done_on", "next_due_on", "updated_at"])
    return assignment
