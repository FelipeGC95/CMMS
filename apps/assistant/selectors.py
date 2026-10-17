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
from dataclasses import dataclass

from django.db import connection
from django.db.models import Q
from django.utils import timezone

from apps.accounts.permissions import Perm, PermissionDeniedError, has_perm
from apps.assets.models import Asset, MeterReading
from apps.assets.selectors import visible_assets
from apps.attachments.selectors import search_manual_pages, search_terms
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
