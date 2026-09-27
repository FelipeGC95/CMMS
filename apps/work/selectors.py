from django.db.models import BooleanField, Case, Count, Exists, F, OuterRef, Q, Value, When
from django.utils import timezone

from apps.accounts.models import User
from apps.accounts.permissions import ROLE_PERMISSIONS, Perm, require_perm
from apps.accounts.scoping import allowed_location_ids
from apps.attachments.models import TargetType
from apps.attachments.selectors import attachments_by_target
from apps.maintenance.models import PlanAssignment, PlanVersion
from apps.organizations.selectors import descendant_ids

from .models import FailureCode, WorkOrder, WorkOrderHelper, WorkRequest

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


def in_locations(location_ids) -> Q:
    """OT o solicitudes cuyo objetivo está en esas ubicaciones: el activo ubicado ahí o la ubicación misma."""
    return Q(asset__location_id__in=location_ids) | Q(location_id__in=location_ids)


def visible_work_orders(*, membership):
    """OT que la membresía puede ver (ORG-06): con alcance, las de sus ubicaciones (y descendientes).

    Una OT sobre una ubicación se ve si esa ubicación está dentro del alcance,
    es decir, si su sitio o alguno de sus ancestros está permitido.
    """
    qs = WorkOrder.objects.all()
    location_ids = allowed_location_ids(membership)
    if location_ids is not None:
        qs = qs.filter(in_locations(location_ids))
    return qs


def assigned_to_user(user) -> Q:
    """OT de la persona: responsable principal o de apoyo ("Mis OT", WO-05 CA1)."""
    return Q(assigned_to=user) | Q(
        Exists(WorkOrderHelper.objects.filter(work_order=OuterRef("pk"), user=user))
    )


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
    qs = visible_work_orders(membership=membership).select_related(
        "asset", "asset__location", "location", "assigned_to"
    )
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
        qs = qs.filter(assigned_to_user(assigned_to))
    if asset is not None:
        qs = qs.filter(asset=asset)
    if asset_q:
        qs = qs.filter(
            Q(asset__code__icontains=asset_q)
            | Q(asset__name__icontains=asset_q)
            | Q(location__code__icontains=asset_q)
            | Q(location__name__icontains=asset_q)
        )
    if location is not None:
        qs = qs.filter(in_locations({location.pk, *descendant_ids(location)}))
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
            "asset",
            "asset__location",
            "location",
            "assigned_to",
            "plan_assignment__plan",
            "plan_assignment__meter",
            "plan_version",
            "failure",
            "parent_work_order",
            "source_checklist_item",
        )
        .prefetch_related(
            "checklist",
            "transitions__by",
            "labor__user",
            "parts",
            "other_costs",
            "helpers",
            "plan_version__resources",
            "absorptions__plan_assignment__plan",
            "absorptions__plan_version",
        )
        .get(pk=pk)
    )


def failure_codes(kind: str):
    return FailureCode.objects.filter(kind=kind, is_active=True)


# --- Solicitudes (REQ-01 CA3) --------------------------------------------------


def visible_requests(*, membership):
    """Solicitudes que la membresía puede ver (ORG-06): alcance por sitio del activo o de la ubicación."""
    qs = WorkRequest.objects.all()
    location_ids = allowed_location_ids(membership)
    if location_ids is not None:
        qs = qs.filter(in_locations(location_ids))
    return qs


def pending_requests(*, membership):
    """Bandeja de solicitudes por decidir, del más antiguo al más nuevo."""
    require_perm(membership, Perm.WORK_ORDER_MANAGE)
    return (
        visible_requests(membership=membership)
        .filter(status=WorkRequest.Status.PENDING)
        .select_related("asset", "location", "requested_by", "work_order")
        .order_by("created_at")
    )


def open_requests_for_target(*, work_request, membership):
    """Otras solicitudes abiertas del mismo activo o ubicación (candidatas para fusionar)."""
    qs = visible_requests(membership=membership).filter(
        asset_id=work_request.asset_id,
        location_id=work_request.location_id,
        status__in=(WorkRequest.Status.PENDING, WorkRequest.Status.ACCEPTED),
    )
    return qs.exclude(pk=work_request.pk).select_related("requested_by").order_by("-created_at")


def failure_code_list(*, kind: str = ""):
    """Catálogo completo (activos e inactivos) para la pantalla de edición (REQ-03 CA1)."""
    qs = FailureCode.objects.all()
    if kind:
        qs = qs.filter(kind=kind)
    return qs.order_by("kind", "name")


# --- Checklist de OT no preventivas (WO-03) ------------------------------------


def checklist_plan_versions(*, work_order):
    """Versiones vigentes (publicadas) de planes activos para aplicar su checklist a la OT.

    Primero las sugeridas: planes asignados al activo o de su clase (`suggested`).
    Una OT sobre una ubicación no tiene sugeridos: se listan todos.
    """
    asset = work_order.asset
    if asset is None:
        suggested = Q(pk__in=[])
    else:
        assigned = PlanAssignment.objects.filter(plan_id=OuterRef("plan_id"), asset=asset, is_active=True)
        suggested = Q(Exists(assigned))
        if asset.asset_class_id:
            suggested |= Q(plan__asset_class_id=asset.asset_class_id)
    return (
        PlanVersion.objects.filter(
            status=PlanVersion.Status.PUBLISHED, plan__is_active=True, plan__current_version=F("pk")
        )
        .select_related("plan")
        .annotate(
            item_count=Count("items"),
            suggested=Case(
                When(suggested, then=Value(True)), default=Value(False), output_field=BooleanField()
            ),
        )
        .filter(item_count__gt=0)
        .order_by("-suggested", "plan__name")
    )


def checklist_photos(*, items) -> dict:
    """Fotos de los ítems del checklist en una sola consulta: {item_id: [adjuntos]}."""
    return attachments_by_target(target_type=TargetType.CHECKLIST_ITEM, target_ids=[i.pk for i in items])
