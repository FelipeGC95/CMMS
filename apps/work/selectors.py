from django.utils import timezone

from .models import FailureCode, WorkOrder

OPEN_STATUSES = [
    WorkOrder.Status.REQUESTED,
    WorkOrder.Status.PLANNED,
    WorkOrder.Status.SCHEDULED,
    WorkOrder.Status.IN_PROGRESS,
    WorkOrder.Status.ON_HOLD,
]


def work_order_list(*, scope: str = "open", work_type: str = "", assigned_to=None, asset=None):
    qs = WorkOrder.objects.select_related("asset", "assigned_to")
    if scope == "open":
        qs = qs.filter(status__in=OPEN_STATUSES)
    elif scope == "overdue":
        qs = qs.filter(status__in=OPEN_STATUSES, due_on__lt=timezone.localdate())
    elif scope == "done":
        qs = qs.filter(status__in=[WorkOrder.Status.COMPLETED, WorkOrder.Status.CLOSED])
    if work_type:
        qs = qs.filter(work_type=work_type)
    if assigned_to is not None:
        qs = qs.filter(assigned_to=assigned_to)
    if asset is not None:
        qs = qs.filter(asset=asset)
    if scope in ("open", "overdue"):
        return qs.order_by("priority", "due_on", "created_at")
    return qs.order_by("-closed_at", "-created_at")


def work_order_detail(pk):
    return (
        WorkOrder.objects.select_related("asset", "asset__location", "assigned_to", "plan_assignment")
        .prefetch_related("checklist", "attachments", "transitions__by", "labor__user")
        .get(pk=pk)
    )


def failure_codes(kind: str):
    return FailureCode.objects.filter(kind=kind, is_active=True)
