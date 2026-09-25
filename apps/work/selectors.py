from django.db.models import Q
from django.utils import timezone

from apps.accounts.models import User
from apps.accounts.permissions import ROLE_PERMISSIONS, Perm, require_perm
from apps.accounts.scoping import allowed_location_ids
from apps.organizations.selectors import descendant_ids

from .models import FailureCode, WorkOrder, WorkRequest

OPEN_STATUSES = [
    WorkOrder.Status.REQUESTED,
    WorkOrder.Status.PLANNED,
    WorkOrder.Status.SCHEDULED,
    WorkOrder.Status.IN_PROGRESS,
    WorkOrder.Status.ON_HOLD,
]

# Campo por el que se puede filtrar el rango de fechas (WO-05 CA1): clave del
# formulario → lookup real (created_at compara por día, no por instante).
DATE_FILTER_LOOKUPS = {"due_on": "due_on", "created_at": "created_at__date"}

ASSIGNABLE_ROLES = [role for role, perms in ROLE_PERMISSIONS.items() if Perm.WORK_ORDER_EXECUTE in perms]


def visible_work_orders(*, membership):
    """OT que la membresía puede ver (ORG-06): las de activos de sus ubicaciones si tiene alcance."""
    qs = WorkOrder.objects.all()
    location_ids = allowed_location_ids(membership)
    if location_ids is not None:
        qs = qs.filter(asset__location_id__in=location_ids)
    return qs


def work_order_list(
    *,
    membership,
    scope: str = "open",
    work_type: str = "",
    status: str = "",
    assigned_to=None,
    asset=None,
    asset_q: str = "",
    location=None,
    date_field: str = "due_on",
    date_from=None,
    date_to=None,
):
    qs = visible_work_orders(membership=membership).select_related("asset", "asset__location", "assigned_to")
    # Un estado puntual (p. ej. "cancelled") manda sobre el balde del scope: si no,
    # nunca sería alcanzable porque ningún scope lo incluye.
    if scope == "overdue":
        qs = qs.filter(due_on__lt=timezone.localdate())
        if not status:
            qs = qs.filter(status__in=OPEN_STATUSES)
    elif scope == "open" and not status:
        qs = qs.filter(status__in=OPEN_STATUSES)
    elif scope == "done" and not status:
        qs = qs.filter(status__in=[WorkOrder.Status.COMPLETED, WorkOrder.Status.CLOSED])
    if status:
        qs = qs.filter(status=status)
    if work_type:
        qs = qs.filter(work_type=work_type)
    if assigned_to is not None:
        qs = qs.filter(assigned_to=assigned_to)
    if asset is not None:
        qs = qs.filter(asset=asset)
    if asset_q:
        qs = qs.filter(Q(asset__code__icontains=asset_q) | Q(asset__name__icontains=asset_q))
    if location is not None:
        qs = qs.filter(asset__location_id__in={location.pk, *descendant_ids(location)})
    lookup = DATE_FILTER_LOOKUPS.get(date_field, DATE_FILTER_LOOKUPS["due_on"])
    if date_from is not None:
        qs = qs.filter(**{f"{lookup}__gte": date_from})
    if date_to is not None:
        qs = qs.filter(**{f"{lookup}__lte": date_to})
    if scope in ("open", "overdue"):
        return qs.order_by("priority", "due_on", "created_at")
    return qs.order_by("-closed_at", "-created_at")


def assignable_members(*, membership):
    """Usuarios que pueden ejecutar OT en la organización activa (para asignar o filtrar por responsable)."""
    return User.objects.filter(
        memberships__organization=membership.organization,
        memberships__role__in=ASSIGNABLE_ROLES,
        memberships__is_active=True,
    ).order_by("full_name", "email")


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


def failure_code_list(*, kind: str = ""):
    """Catálogo completo (activos e inactivos) para la pantalla de edición (REQ-03 CA1)."""
    qs = FailureCode.objects.all()
    if kind:
        qs = qs.filter(kind=kind)
    return qs.order_by("kind", "name")
