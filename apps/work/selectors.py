from django.utils import timezone

from apps.accounts.permissions import Perm, require_perm
from apps.accounts.scoping import allowed_location_ids

from .models import FailureCode, WorkOrder, WorkRequest

OPEN_STATUSES = [
    WorkOrder.Status.REQUESTED,
    WorkOrder.Status.PLANNED,
    WorkOrder.Status.SCHEDULED,
    WorkOrder.Status.IN_PROGRESS,
    WorkOrder.Status.ON_HOLD,
]


def visible_work_orders(*, membership):
    """OT que la membresía puede ver (ORG-06): las de activos de sus ubicaciones si tiene alcance."""
    qs = WorkOrder.objects.all()
    location_ids = allowed_location_ids(membership)
    if location_ids is not None:
        qs = qs.filter(asset__location_id__in=location_ids)
    return qs


def work_order_list(*, membership, scope: str = "open", work_type: str = "", assigned_to=None, asset=None):
    qs = visible_work_orders(membership=membership).select_related("asset", "assigned_to")
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


def work_order_detail(pk, *, membership):
    return (
        visible_work_orders(membership=membership)
        .select_related(
            "asset", "asset__location", "assigned_to", "plan_assignment__plan", "plan_assignment__meter"
        )
        .prefetch_related(
            "checklist", "attachments", "transitions__by", "labor__user", "parts", "other_costs"
        )
        .get(pk=pk)
    )


def failure_codes(kind: str):
    return FailureCode.objects.filter(kind=kind, is_active=True)


# --- Solicitudes (REQ-01 CA3) --------------------------------------------------


def visible_requests(*, membership):
    """Solicitudes que la membresía puede ver (ORG-06): alcance por sitio del activo."""
    qs = WorkRequest.objects.all()
    location_ids = allowed_location_ids(membership)
    if location_ids is not None:
        qs = qs.filter(asset__location_id__in=location_ids)
    return qs


def pending_requests(*, membership):
    """Bandeja de solicitudes por decidir, del más antiguo al más nuevo."""
    require_perm(membership, Perm.WORK_ORDER_MANAGE)
    return (
        visible_requests(membership=membership)
        .filter(status=WorkRequest.Status.PENDING)
        .select_related("asset", "requested_by", "work_order")
        .order_by("created_at")
    )


def open_requests_for_asset(*, asset, exclude=None):
    """Otras solicitudes del mismo activo aún abiertas (candidatas para fusionar)."""
    qs = WorkRequest.objects.filter(
        asset=asset, status__in=(WorkRequest.Status.PENDING, WorkRequest.Status.ACCEPTED)
    )
    if exclude is not None:
        qs = qs.exclude(pk=exclude.pk)
    return qs.select_related("requested_by").order_by("-created_at")
