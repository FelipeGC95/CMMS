"""Lecturas del asistente de diagnóstico (AI-01..05, ADR-018).

Conversaciones: cada usuario ve las suyas; el administrador (`assistant.view_all`) las de
toda la organización. Siempre con el alcance por sitio (ORG-06): una conversación sobre un
activo fuera del alcance no se ve.

Búsquedas de las herramientas: reciben la `membership` de quien pregunta y parten de los
selectores con alcance (`visible_work_orders`, `visible_assets`, `search_manual_pages`): el
aislamiento lo garantiza este código, no el modelo. PostgreSQL usa texto completo en
español (`SearchVector`/`SearchQuery` con configuración `spanish`, calculado al consultar:
sin índice GIN en v1); otros motores (SQLite local), `icontains`.
"""

import datetime
import zoneinfo
from dataclasses import dataclass, field
from decimal import Decimal

from django.conf import settings
from django.db import connection
from django.db.models import Case, Count, Q, Sum, Value, When
from django.db.models.functions import TruncMonth
from django.utils import timezone

from apps.accounts.permissions import Perm, PermissionDeniedError, has_perm
from apps.assets.models import Asset, MeterReading
from apps.assets.selectors import visible_assets
from apps.attachments.selectors import search_manual_pages, search_terms
from apps.organizations.models import Organization
from apps.work.models import FailureEvent, WorkOrder
from apps.work.selectors import visible_work_orders

from .models import AssistantConversation, AssistantMessage

RESULT_LIMIT = 8
READINGS_LIMIT = 20
FAILURES_LIMIT = 10


def can_use(membership) -> bool:
    return has_perm(membership, Perm.ASSISTANT_USE)


def can_view_all(membership) -> bool:
    return has_perm(membership, Perm.ASSISTANT_VIEW_ALL)


# --- Conversaciones --------------------------------------------------------------------------


def visible_conversations(*, membership):
    """Conversaciones que la membresía puede leer: las suyas (o todas, con `view_all`), con alcance."""
    if not (can_use(membership) or can_view_all(membership)):
        raise PermissionDeniedError(Perm.ASSISTANT_USE)
    qs = AssistantConversation.objects.filter(asset__in=visible_assets(membership=membership))
    if not can_view_all(membership):
        qs = qs.filter(started_by=membership.user)
    return qs


def conversation_list(*, membership, mine_only: bool = False, asset=None, work_order=None):
    """Lista paginable (la vista pagina): más recientes primero, sin N+1."""
    qs = visible_conversations(membership=membership)
    if mine_only:
        qs = qs.filter(started_by=membership.user)
    if asset is not None:
        qs = qs.filter(asset=asset)
    if work_order is not None:
        qs = qs.filter(work_order=work_order)
    return qs.select_related("asset", "work_order", "started_by").order_by("-created_at")


def conversation_for(*, membership, pk) -> AssistantConversation | None:
    return (
        visible_conversations(membership=membership)
        .select_related("asset__asset_class", "asset__location", "work_order", "started_by")
        .filter(pk=pk)
        .first()
    )


def closing_conversation(*, membership, work_order) -> AssistantConversation | None:
    """Conversación propia sobre la OT que aporta al cierre (AI-04): la más reciente con nota
    propuesta o, si ninguna la tiene, la más reciente. None sin permiso o sin conversación."""
    if not can_use(membership) or work_order.asset_id is None:
        return None
    with_summary = Case(
        When(summary_status=AssistantConversation.SummaryStatus.DONE, then=Value(0)), default=Value(1)
    )
    return (
        visible_conversations(membership=membership)
        .filter(work_order=work_order, started_by=membership.user)
        .order_by(with_summary, "-created_at")
        .first()
    )


def conversation_messages(*, conversation):
    """Mensajes del historial (sin la nota de cierre), en orden."""
    return AssistantMessage.objects.filter(
        conversation=conversation, kind=AssistantMessage.Kind.TURN
    ).order_by("seq")


def has_pending(*, conversation) -> bool:
    """¿Hay un turno o una nota en preparación? (la página consulta mientras sea así)."""
    return AssistantMessage.objects.filter(
        conversation=conversation, status=AssistantMessage.Status.PENDING
    ).exists()


def message_is_visible(*, membership, message_id) -> bool:
    """Para servir la foto de una pregunta (`TargetType.ASSISTANT_MESSAGE`)."""
    if not (can_use(membership) or can_view_all(membership)):
        return False
    return AssistantMessage.objects.filter(
        pk=message_id, conversation__in=visible_conversations(membership=membership)
    ).exists()


def message_photos(*, messages) -> dict:
    """{id del mensaje: [fotos]} en una consulta."""
    from apps.attachments.models import TargetType
    from apps.attachments.selectors import attachments_by_target

    return attachments_by_target(
        target_type=TargetType.ASSISTANT_MESSAGE, target_ids=[message.pk for message in messages]
    )


# --- Cupo mensual (AI-05 CA1) ----------------------------------------------------------------


def month_start(organization, now=None) -> datetime.datetime:
    """Inicio del mes calendario en la zona de la organización (como instante UTC)."""
    tz = zoneinfo.ZoneInfo(organization.timezone or "UTC")
    local = (now or timezone.now()).astimezone(tz)
    return local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


@dataclass(frozen=True)
class QuotaStatus:
    used: int
    limit: int | None  # None = sin límite

    @property
    def remaining(self) -> int | None:
        return None if self.limit is None else max(self.limit - self.used, 0)

    @property
    def exhausted(self) -> bool:
        return self.limit is not None and self.used >= self.limit


def quota_status(*, organization, now=None) -> QuotaStatus:
    """Conversaciones creadas este mes contra el cupo del plan (sin plan = sin límite)."""
    used = AssistantConversation.objects.filter(created_at__gte=month_start(organization, now)).count()
    limit = organization.plan.max_assistant_conversations if organization.plan_id else None
    return QuotaStatus(used=used, limit=limit)


# --- Búsquedas de las herramientas (ADR-018) --------------------------------------------------


def asset_by_code(*, membership, code: str) -> Asset | None:
    code = (code or "").strip()
    if not code:
        return None
    return visible_assets(membership=membership).filter(code__iexact=code).first()


WORK_ORDER_TEXT_FIELDS = ("title", "description", "completion_notes", "failure__description")


def search_work_orders(*, membership, query: str, asset=None, limit: int = RESULT_LIMIT):
    """OT visibles cuyo título, descripción, cierre o síntoma contienen la consulta.

    Basta con que aparezca alguna palabra (el modelo busca con sus propias palabras);
    PostgreSQL ordena por relevancia y luego por fecha. Sin las canceladas.
    """
    terms = search_terms(query)
    qs = (
        visible_work_orders(membership=membership)
        .exclude(status=WorkOrder.Status.CANCELLED)
        .select_related("asset", "location", "failure__failure_mode", "failure__failure_cause")
    )
    if asset is not None:
        qs = qs.filter(asset=asset)
    if not terms:
        return list(qs.none())
    if connection.vendor == "postgresql":
        from django.contrib.postgres.search import SearchQuery, SearchRank, SearchVector

        vector = SearchVector(*WORK_ORDER_TEXT_FIELDS, config="spanish")
        search = SearchQuery(" or ".join(terms), config="spanish", search_type="websearch")
        qs = (
            qs.annotate(search=vector, rank=SearchRank(vector, search))
            .filter(search=search)
            .order_by("-rank", "-created_at")
        )
    else:
        match = Q()
        for term in terms:
            for field in WORK_ORDER_TEXT_FIELDS:
                match |= Q(**{f"{field}__icontains": term})
        qs = qs.filter(match).order_by("-created_at")
    return list(qs[:limit])


def asset_failures(*, membership, asset, limit: int = FAILURES_LIMIT):
    """Últimas fallas del activo (o en las que fue el componente), si el activo es visible."""
    assets = visible_assets(membership=membership)
    if not assets.filter(pk=asset.pk).exists():
        return []
    return list(
        FailureEvent.objects.filter(Q(asset=asset) | Q(component=asset))
        .filter(asset__in=assets)  # la falla de un equipo padre fuera del alcance no se ve
        .select_related("work_order", "failure_mode", "failure_cause", "component")
        .order_by("-started_at")[:limit]
    )


def recent_meter_readings(*, membership, asset, limit: int = READINGS_LIMIT):
    """Últimas lecturas de los medidores y variables activos del activo, si es visible."""
    if not visible_assets(membership=membership).filter(pk=asset.pk).exists():
        return []
    return list(
        MeterReading.objects.filter(meter__asset=asset, meter__is_active=True)
        .select_related("meter")
        .order_by("-read_at")[:limit]
    )


def manual_pages(*, membership, query: str, limit: int = RESULT_LIMIT):
    """Páginas de manuales visibles (AST-06 CA3), sobre `search_manual_pages`."""
    return list(search_manual_pages(membership=membership, query=query)[:limit])


def open_work_orders(*, membership, asset, limit: int = RESULT_LIMIT):
    """OT abiertas del activo, para el contexto del primer turno (AI-01 CA2)."""
    open_statuses = [
        s
        for s in WorkOrder.Status.values
        if s not in (*WorkOrder.CLOSED_STATUSES, WorkOrder.Status.COMPLETED)
    ]
    return list(
        visible_work_orders(membership=membership)
        .filter(asset=asset, status__in=open_statuses)
        .order_by("-created_at")[:limit]
    )


# --- Costo por organización y mes (AI-05 CA2) -------------------------------------------------

MILLION = Decimal(1_000_000)
USAGE_MONTHS = 12
TOKEN_FIELDS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens")


def message_cost_usd(
    *, model: str, input_tokens: int, output_tokens: int, cache_read_tokens: int, cache_creation_tokens: int
) -> Decimal | None:
    """Costo estimado en USD con los precios por millón de `AI_MODEL_PRICES_USD`; None sin precio.

    `input_tokens` de la API no incluye lo leído ni lo escrito en el caché: cada parte tiene su precio.
    """
    prices = settings.AI_MODEL_PRICES_USD.get(model)
    if prices is None:
        return None
    total = (
        input_tokens * Decimal(str(prices["input"]))
        + output_tokens * Decimal(str(prices["output"]))
        + cache_read_tokens * Decimal(str(prices["cache_read"]))
        + cache_creation_tokens * Decimal(str(prices["cache_write"]))
    )
    return total / MILLION


@dataclass
class UsageRow:
    organization_id: object
    month: datetime.date
    organization: str = ""
    conversations: int = 0
    replies: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    cost_usd: Decimal = Decimal(0)
    models: list = field(default_factory=list)
    unpriced: list = field(default_factory=list)  # modelos sin precio: el costo queda incompleto


def usage_months_start(now=None, months: int = USAGE_MONTHS) -> datetime.datetime:
    """Inicio (UTC) del mes calendario de hace `months - 1` meses."""
    start = (now or timezone.now()).astimezone(datetime.UTC)
    start = start.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    for _month in range(months - 1):
        start = (start - datetime.timedelta(days=1)).replace(day=1)
    return start


def usage_by_organization_month(*, months: int = USAGE_MONTHS, now=None) -> list[UsageRow]:
    """Tokens y costo estimado por organización y mes calendario (UTC), del mes más reciente al
    más antiguo y, dentro del mes, de mayor a menor costo.

    Solo para el superadmin de plataforma (admin de Django). Suma todas las respuestas del
    asistente con tokens (turnos y notas de cierre, también las fallidas: la API las cobra igual).
    """
    start = usage_months_start(now, months)
    month = TruncMonth("created_at", tzinfo=datetime.UTC)
    # Plataforma (AI-05 CA2): el superadmin ve el costo de todas las organizaciones, a propósito
    # sin contexto de tenant; `all_tenants` desactiva RLS solo para estas lecturas agregadas.
    messages = (
        AssistantMessage.all_tenants.filter(created_at__gte=start, role=AssistantMessage.Role.ASSISTANT)
        .annotate(month=month)
        .values("organization_id", "month", "model")
        .annotate(replies=Count("pk"), **{name: Sum(name) for name in TOKEN_FIELDS})
        .order_by()
    )
    conversations = (
        AssistantConversation.all_tenants.filter(created_at__gte=start)
        .annotate(month=month)
        .values("organization_id", "month")
        .annotate(count=Count("pk"))
        .order_by()
    )
    rows: dict = {}

    def row_for(organization_id, month_start) -> UsageRow:
        key = (organization_id, month_start.date())
        if key not in rows:
            rows[key] = UsageRow(organization_id=organization_id, month=key[1])
        return rows[key]

    for item in messages:
        row = row_for(item["organization_id"], item["month"])
        tokens = {name: item[name] or 0 for name in TOKEN_FIELDS}
        row.replies += item["replies"]
        for name, value in tokens.items():
            setattr(row, name, getattr(row, name) + value)
        model = item["model"] or ""
        if model and model not in row.models:
            row.models.append(model)
        cost = message_cost_usd(model=model, **tokens)
        if cost is not None:
            row.cost_usd += cost
        elif any(tokens.values()):
            row.unpriced.append(model or "?")
    for item in conversations:
        row_for(item["organization_id"], item["month"]).conversations += item["count"]

    names = dict(Organization.objects.filter(pk__in={key[0] for key in rows}).values_list("pk", "name"))
    for row in rows.values():
        row.organization = names.get(row.organization_id, str(row.organization_id))
        row.models.sort()
    return sorted(rows.values(), key=lambda row: (-row.month.toordinal(), -row.cost_usd, row.organization))
