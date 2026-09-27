import datetime
from dataclasses import dataclass
from decimal import Decimal

from django.db.models import (
    BooleanField,
    Case,
    Count,
    Exists,
    F,
    IntegerField,
    OuterRef,
    Q,
    Subquery,
    Sum,
    Value,
    When,
)
from django.utils import timezone

from apps.accounts.models import Membership, User
from apps.accounts.permissions import ROLE_PERMISSIONS, Perm, require_perm
from apps.accounts.scoping import allowed_location_ids, is_scoped
from apps.attachments.models import Attachment, Purpose, TargetType
from apps.attachments.selectors import attachments_by_target
from apps.maintenance.models import PlanAssignment, PlanVersion
from apps.organizations.models import Location
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


@dataclass
class WorkOrderListQuery:
    """Lista de OT (WO-05) armada desde la URL: la usan la pantalla y el Excel."""

    queryset: object
    filter_form: object
    scope: str
    work_type: str

    @property
    def filters(self) -> dict:
        return self.filter_form.cleaned_data


def filter_work_orders(*, params, membership) -> WorkOrderListQuery:
    """Lee vista (scope), tipo y filtros de `params` (request.GET) y devuelve la consulta.

    El técnico entra por defecto a «Mis OT». Los campos con error se ignoran y
    quedan en el formulario para mostrarlos.
    """
    from .forms import WorkOrderFilterForm  # forms importa este módulo

    default_scope = "mine" if membership.role == Membership.Role.TECHNICIAN else "open"
    scope = params.get("scope", default_scope)
    work_type = params.get("type", "")
    filter_form = WorkOrderFilterForm(params, membership=membership)
    filter_form.is_valid()
    filters = filter_form.cleaned_data
    queryset = work_order_list(
        membership=membership,
        scope="open" if scope == "mine" else scope,
        work_type=work_type,
        status=filters.get("status", ""),
        assigned_to=membership.user if scope == "mine" else filters.get("assigned_to"),
        asset_q=filters.get("asset_q", ""),
        location=filters.get("location"),
        date_field=filters.get("date_field") or "due_on",
        date_from=filters.get("date_from"),
        date_to=filters.get("date_to"),
    )
    return WorkOrderListQuery(queryset=queryset, filter_form=filter_form, scope=scope, work_type=work_type)


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


def anonymous_request_photos(work_request):
    """Fotos de un aviso anónimo que aún no tiene OT (ver `services.report_anonymous_failure`)."""
    return Attachment.objects.filter(
        target_type=TargetType.WORK_REQUEST, target_id=work_request.pk, purpose=Purpose.PHOTO
    ).order_by("created_at")


def pending_requests(*, membership):
    """Bandeja de solicitudes por decidir, del más antiguo al más nuevo.

    `photo_id`: la primera foto de un aviso anónimo (REQ-02), que aún no tiene OT.
    """
    require_perm(membership, Perm.WORK_ORDER_MANAGE)
    photos = Attachment.objects.filter(
        target_type=TargetType.WORK_REQUEST, target_id=OuterRef("pk"), purpose=Purpose.PHOTO
    ).order_by("created_at")
    return (
        visible_requests(membership=membership)
        .filter(status=WorkRequest.Status.PENDING)
        .select_related("asset", "location", "requested_by", "work_order")
        .annotate(photo_id=Subquery(photos.values("pk")[:1]))
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


# --- Backlog priorizado y programación semanal (REQ-04, REQ-05) -----------------

# Puntaje del backlog (REQ-04 CA1) = peso de criticidad × peso de prioridad (1 a 12).
CRITICALITY_WEIGHTS = {"A": 3, "B": 2, "C": 1}
# Una OT sobre una ubicación no tiene activo: se toma como criticidad media (B).
LOCATION_CRITICALITY_WEIGHT = 2
PRIORITY_WEIGHTS = {1: 4, 2: 3, 3: 2, 4: 1}  # urgente, alta, normal, baja

# La capacidad semanal se reparte de lunes a viernes; el fin de semana no tiene capacidad.
WORKDAYS_PER_WEEK = 5

# Estados que se pueden programar: la ejecución aún no empieza (REQ-05 CA2).
SCHEDULABLE_STATUSES = (WorkOrder.Status.REQUESTED, WorkOrder.Status.PLANNED, WorkOrder.Status.SCHEDULED)


def backlog_score() -> Case:
    """Expresión SQL del puntaje del backlog (criticidad × prioridad)."""
    whens = [
        When(asset__criticality=crit, priority=prio, then=Value(c_weight * p_weight))
        for crit, c_weight in CRITICALITY_WEIGHTS.items()
        for prio, p_weight in PRIORITY_WEIGHTS.items()
    ] + [
        When(asset__isnull=True, priority=prio, then=Value(LOCATION_CRITICALITY_WEIGHT * p_weight))
        for prio, p_weight in PRIORITY_WEIGHTS.items()
    ]
    return Case(*whens, default=Value(0), output_field=IntegerField())


BACKLOG_ORDER = ("-score", F("due_on").asc(nulls_last=True), "created_at")


def backlog(*, membership, location=None, work_type: str = "", status: str = "", assigned_to=None):
    """OT pendientes ordenadas por puntaje (REQ-04 CA1), dentro del alcance de la membresía.

    Pendiente = abierta (ni completada, ni cerrada, ni cancelada): una OT completada que
    espera aprobación ya no tiene trabajo por hacer. Desempate: fecha límite (sin fecha al
    final) y luego antigüedad.
    """
    require_perm(membership, Perm.WORK_ORDER_MANAGE)
    qs = visible_work_orders(membership=membership).filter(status__in=OPEN_STATUSES)
    if status:
        qs = qs.filter(status=status)
    if work_type:
        qs = qs.filter(work_type=work_type)
    if assigned_to is not None:
        qs = qs.filter(assigned_to_user(assigned_to))
    if location is not None:
        qs = qs.filter(in_locations({location.pk, *descendant_ids(location)}))
    return (
        qs.select_related("asset", "asset__location", "location", "assigned_to")
        .annotate(score=backlog_score())
        .order_by(*BACKLOG_ORDER)
    )


def filter_backlog(*, params, membership):
    """Backlog con los filtros de la URL (sitio, tipo, estado, responsable): pantalla y Excel.

    Devuelve `(consulta, formulario)`; los campos con error se ignoran y quedan en el formulario.
    """
    from .forms import BacklogFilterForm  # forms importa este módulo

    form = BacklogFilterForm(params, membership=membership)
    form.is_valid()
    filters = form.cleaned_data
    queryset = backlog(
        membership=membership,
        location=filters.get("location"),
        work_type=filters.get("work_type", ""),
        status=filters.get("status", ""),
        assigned_to=filters.get("assigned_to"),
    )
    return queryset, form


def _with_descendants(roots, children) -> set:
    found, pending = set(roots), list(roots)
    while pending:
        for child in children.get(pending.pop(), []):
            if child not in found:
                found.add(child)
                pending.append(child)
    return found


def technicians_in_scope(*, membership, location=None) -> list:
    """Técnicos activos que trabajan en lo que se mira (capacidad del equipo, REQ-04 CA2 / REQ-05).

    Un técnico sin alcance por sitio cuenta en todas partes; con alcance, cuenta si
    alguna de sus ubicaciones (o descendientes) cae dentro del sitio filtrado y del
    alcance de quien mira. Devuelve membresías con su usuario.
    """
    technicians = list(
        Membership.objects.filter(
            organization=membership.organization,
            role=Membership.Role.TECHNICIAN,
            is_active=True,
            user__is_active=True,
        )
        .select_related("user")
        .prefetch_related("scope")
        .order_by("user__full_name", "user__email")
    )
    viewer_ids = allowed_location_ids(membership)
    if location is None and viewer_ids is None:
        return technicians
    children: dict = {}
    for location_id, parent_id in Location.objects.values_list("id", "parent_id"):
        children.setdefault(parent_id, []).append(location_id)
    wanted = _with_descendants([location.pk], children) if location is not None else None
    if viewer_ids is not None:
        wanted = set(viewer_ids) if wanted is None else wanted & set(viewer_ids)
    return [
        t
        for t in technicians
        if not is_scoped(t) or _with_descendants([s.location_id for s in t.scope.all()], children) & wanted
    ]


def _hours(minutes) -> Decimal:
    return (Decimal(minutes or 0) / 60).quantize(Decimal("0.1"))


@dataclass
class BacklogSummary:
    count: int
    estimated_hours: Decimal
    unestimated: int
    weekly_capacity_hours: Decimal
    technicians: int

    @property
    def weeks(self) -> Decimal | None:
        """Backlog en semanas (REQ-04 CA2) = horas estimadas / capacidad semanal del equipo."""
        if not self.weekly_capacity_hours:
            return None
        return (self.estimated_hours / self.weekly_capacity_hours).quantize(Decimal("0.1"))


def backlog_summary(queryset, *, technicians) -> BacklogSummary:
    """Totales del backlog filtrado en una consulta; las OT sin estimación no suman horas."""
    totals = queryset.order_by().aggregate(
        count=Count("pk"),
        minutes=Sum("estimated_minutes"),
        unestimated=Count("pk", filter=Q(estimated_minutes__isnull=True)),
    )
    return BacklogSummary(
        count=totals["count"],
        estimated_hours=_hours(totals["minutes"]),
        unestimated=totals["unestimated"],
        weekly_capacity_hours=sum((t.weekly_capacity_hours for t in technicians), Decimal(0)),
        technicians=len(technicians),
    )


def week_start(day: datetime.date) -> datetime.date:
    """Lunes de la semana de `day`."""
    return day - datetime.timedelta(days=day.weekday())


def scheduled_in_week(*, membership, monday, location=None, user=None):
    """OT programadas de lunes a domingo (sin las canceladas), por día y puntaje.

    `user` limita a las OT de esa persona (responsable o apoyo): «Mi semana».
    """
    qs = visible_work_orders(membership=membership).filter(
        scheduled_for__gte=monday, scheduled_for__lte=monday + datetime.timedelta(days=6)
    )
    qs = qs.exclude(status=WorkOrder.Status.CANCELLED)
    if user is not None:
        qs = qs.filter(assigned_to_user(user))
    if location is not None:
        qs = qs.filter(in_locations({location.pk, *descendant_ids(location)}))
    return (
        qs.select_related("asset", "asset__location", "location", "assigned_to")
        .annotate(score=backlog_score())
        .order_by("scheduled_for", *BACKLOG_ORDER)
    )


def unscheduled_backlog(*, membership, location=None):
    """Backlog sin día asignado: lo que falta ubicar en el tablero (REQ-05 CA1)."""
    return backlog(membership=membership, location=location).filter(
        status__in=SCHEDULABLE_STATUSES, scheduled_for__isnull=True
    )


@dataclass
class LoadCell:
    minutes: int
    capacity_minutes: int

    @property
    def over(self) -> bool:
        return self.minutes > self.capacity_minutes

    @property
    def hours(self) -> Decimal:
        return _hours(self.minutes)

    @property
    def capacity_hours(self) -> Decimal:
        return _hours(self.capacity_minutes)


@dataclass
class LoadRow:
    user: object
    days: list
    week: LoadCell


def daily_capacity_minutes(membership, day: datetime.date) -> int:
    """Capacidad del día: la semanal repartida de lunes a viernes; sábado y domingo, cero."""
    if day.weekday() >= WORKDAYS_PER_WEEK:
        return 0
    return int(membership.weekly_capacity_hours * 60 / WORKDAYS_PER_WEEK)


def technician_load(*, work_orders, technicians, days) -> list[LoadRow]:
    """Carga vs. capacidad por técnico y día (REQ-05 CA1), a partir de las OT ya consultadas.

    Suma la estimación de las OT donde el técnico es responsable principal; el apoyo
    no suma carga (la estimación es de la OT, no por persona).
    """
    minutes: dict = {}
    for wo in work_orders:
        key = (wo.assigned_to_id, wo.scheduled_for)
        minutes[key] = minutes.get(key, 0) + (wo.estimated_minutes or 0)
    rows = []
    for technician in technicians:
        cells = [
            LoadCell(minutes.get((technician.user_id, day), 0), daily_capacity_minutes(technician, day))
            for day in days
        ]
        week = LoadCell(sum(c.minutes for c in cells), int(technician.weekly_capacity_hours * 60))
        rows.append(LoadRow(user=technician.user, days=cells, week=week))
    return rows


@dataclass
class LaneCell:
    day: datetime.date
    work_orders: list
    load: LoadCell | None


@dataclass
class Lane:
    """Fila del tablero por técnico (REQ-05 CA1): `user` None = OT sin responsable."""

    user: object
    cells: list
    week: LoadCell | None


def board_lanes(*, work_orders, loads, days, visible_days) -> list[Lane]:
    """Una fila por técnico en alcance (con su carga por día), otros responsables y «Sin asignar».

    Trabaja sobre las OT y la carga ya consultadas (`loads` de `technician_load` con
    `days`): no agrega consultas por técnico. Un responsable que no es técnico en
    alcance (p. ej. un supervisor) tiene su propia fila, sin capacidad, para que
    ninguna OT programada quede fuera del tablero.
    """
    grouped: dict = {}
    others: dict = {}
    for wo in work_orders:
        grouped.setdefault((wo.assigned_to_id, wo.scheduled_for), []).append(wo)
        if wo.assigned_to_id is not None:
            others.setdefault(wo.assigned_to_id, wo.assigned_to)
    lanes = []
    for row in loads:
        others.pop(row.user.pk, None)
        by_day = dict(zip(days, row.days, strict=True))
        cells = [LaneCell(day, grouped.get((row.user.pk, day), []), by_day[day]) for day in visible_days]
        lanes.append(Lane(user=row.user, cells=cells, week=row.week))
    for user_id, user in sorted(others.items(), key=lambda item: str(item[1])):
        cells = [LaneCell(day, grouped.get((user_id, day), []), None) for day in visible_days]
        lanes.append(Lane(user=user, cells=cells, week=None))
    cells = [LaneCell(day, grouped.get((None, day), []), None) for day in visible_days]
    lanes.append(Lane(user=None, cells=cells, week=None))
    return lanes
