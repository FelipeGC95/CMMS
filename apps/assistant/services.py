"""Asistente de diagnóstico con IA (AI-01..05, ADR-018, docs/03 §2.12).

Flujo de un turno: `start_conversation` / `ask` guardan la pregunta y un mensaje del
asistente `pending`, y al confirmar la transacción programan `run_turn` (en el worker con
`AI_ASSISTANT_USE_QUEUE`, si no en la misma petición). La página consulta mientras haya
mensajes pendientes.

`run_turn` arma el historial con los mensajes guardados **tal cual** (append-only: el
razonamiento preservado de Opus 5.5 exige no editar turnos anteriores), llama a la API en un
bucle manual de herramientas (máx. `AI_ASSISTANT_MAX_TOOL_ROUNDS` rondas), ejecuta cada
herramienta con la membresía de quien pregunta y guarda el turno completo, sus tokens y las
fuentes citadas que el backend validó. Errores, rechazos y respuestas truncadas dejan el
mensaje en `error`/`refused` con `content` vacío: no entran al historial.

Cupo (AI-05 CA1): conversaciones nuevas por mes calendario (zona de la organización) según el
plan; al agotarse no se crea la conversación y el resto del sistema sigue igual.
"""

import base64
import datetime
import logging
from dataclasses import dataclass, field

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Max
from django.utils import timezone
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy

from apps.accounts.models import Membership
from apps.accounts.permissions import Perm, PermissionDeniedError, has_perm, require_perm
from apps.assets.selectors import visible_assets
from apps.attachments.models import Attachment, Purpose, TargetType
from apps.attachments.services import IMAGE_CONTENT_TYPES, add_attachment
from apps.audit import services as audit
from apps.audit.models import AuditEvent
from apps.core.tenancy import require_current_organization, tenant_context
from apps.organizations.models import Organization
from apps.organizations.modules import ModuleNotIncludedError, has_module
from apps.work.selectors import visible_work_orders

from . import llm, tools
from .citations import validate_citations
from .models import AssistantConversation, AssistantMessage
from .selectors import quota_status

logger = logging.getLogger(__name__)

MODULE = "assistant"
MAX_QUESTION_CHARS = 4000
TITLE_CHARS = 80
STALE_AFTER = datetime.timedelta(minutes=15)
Status = AssistantMessage.Status
Role = AssistantMessage.Role
Kind = AssistantMessage.Kind

# Códigos de `AssistantMessage.error_code` → texto para la pantalla (A2).
ERROR_MESSAGES = {
    "not_configured": gettext_lazy("El asistente no está configurado. Avísale al administrador."),
    "rate_limited": gettext_lazy("El asistente está con mucha demanda. Inténtalo de nuevo en un minuto."),
    "overloaded": gettext_lazy("El servicio del asistente no está disponible. Inténtalo de nuevo más tarde."),
    "api_error": gettext_lazy("El asistente no pudo responder esta pregunta. Inténtalo de nuevo."),
    "connection": gettext_lazy("No hubo conexión con el servicio del asistente. Inténtalo de nuevo."),
    "max_tokens": gettext_lazy("La respuesta quedó demasiado larga. Haz una pregunta más acotada."),
    "too_many_tools": gettext_lazy("El asistente no llegó a una respuesta. Reformula la pregunta."),
    "refused": gettext_lazy("El asistente no puede responder esta pregunta."),
    "no_access": gettext_lazy("Ya no tienes acceso al asistente en esta organización."),
    "module": gettext_lazy("El asistente no está incluido en el plan de la organización."),
    "timeout": gettext_lazy("La respuesta tardó demasiado. Inténtalo de nuevo."),
    "internal": gettext_lazy("Ocurrió un error inesperado. Inténtalo de nuevo."),
}
LIMIT_REACHED = "Límite de consultas alcanzado en este turno: responde ya con la información reunida."


class AssistantError(ValidationError):
    """Pregunta inválida o acción no permitida en la conversación."""


class AssistantNotConfiguredError(AssistantError):
    """Sin `ANTHROPIC_API_KEY`: se avisa en vez de fallar (D3)."""


class AssistantQuotaExceededError(AssistantError):
    """Se agotó el cupo mensual de conversaciones del plan (AI-05 CA1)."""


def module_enabled(organization=None) -> bool:
    return has_module(organization or require_current_organization(), MODULE)


def is_configured() -> bool:
    return llm.is_configured()


def _require_access(membership) -> None:
    organization = require_current_organization()
    if not has_module(organization, MODULE):
        raise ModuleNotIncludedError(organization, MODULE)
    require_perm(membership, Perm.ASSISTANT_USE)


def _require_configured() -> None:
    if not llm.is_configured():
        raise AssistantNotConfiguredError(ERROR_MESSAGES["not_configured"])


def _require_owner(membership, conversation) -> None:
    """Solo quien la inició pregunta, pide la nota o marca si sirvió (el admin solo la consulta)."""
    if conversation.started_by_id != membership.user_id:
        raise PermissionDeniedError(Perm.ASSISTANT_USE)
    if not visible_assets(membership=membership).filter(pk=conversation.asset_id).exists():
        raise PermissionDeniedError(Perm.ASSISTANT_USE)


def _clean_text(text) -> str:
    text = (text or "").strip()
    if len(text) > MAX_QUESTION_CHARS:
        raise AssistantError(_("Máximo %(n)s caracteres.") % {"n": MAX_QUESTION_CHARS})
    return text


def _title(text: str, asset) -> str:
    first_line = text.splitlines()[0].strip() if text else ""
    if not first_line:
        return _("Consulta sobre %(asset)s") % {"asset": asset.code}
    return first_line if len(first_line) <= TITLE_CHARS else first_line[: TITLE_CHARS - 1] + "…"


# --- Conversaciones y preguntas (AI-01) ------------------------------------------------------


def start_conversation(
    *, membership, asset, text: str = "", work_order=None, photo=None, client_uuid=None
) -> AssistantConversation:
    """Abre una conversación sobre un activo visible (y su OT, si aplica) con la primera pregunta.

    Cuenta para el cupo mensual del plan. Idempotente por `client_uuid`.
    """
    if client_uuid:
        existing = AssistantConversation.objects.filter(client_uuid=client_uuid).first()
        if existing is not None:
            _require_owner(membership, existing)
            return existing
    _require_access(membership)
    _require_configured()
    if not visible_assets(membership=membership).filter(pk=asset.pk).exists():
        raise PermissionDeniedError(Perm.ASSISTANT_USE)
    if work_order is not None:
        if work_order.asset_id != asset.pk:
            raise AssistantError(_("La OT no corresponde a este activo."))
        if not visible_work_orders(membership=membership).filter(pk=work_order.pk).exists():
            raise PermissionDeniedError(Perm.ASSISTANT_USE)
    text = _clean_text(text)
    if not text and photo is None:
        raise AssistantError(_("Describe el síntoma o agrega una foto."))

    organization = require_current_organization()
    with transaction.atomic():
        # Bloquea la organización: dos conversaciones simultáneas no superan el cupo entre ambas.
        Organization.objects.select_for_update().filter(pk=organization.pk).values_list("pk").get()
        quota = quota_status(organization=organization)
        if quota.exhausted:
            raise AssistantQuotaExceededError(
                _(
                    "Se usaron las %(limit)s conversaciones del asistente de este mes. El resto del "
                    "sistema sigue funcionando; pide al administrador ampliar el plan."
                )
                % {"limit": quota.limit}
            )
        conversation = AssistantConversation.objects.create(
            asset=asset,
            work_order=work_order,
            started_by=membership.user,
            title=_title(text, asset),
            model=settings.AI_ASSISTANT_MODEL,
            client_uuid=client_uuid,
        )
        audit.record(action=AuditEvent.Action.CREATE, target=conversation, membership=membership)
        context, context_sources = tools.asset_context(
            membership=membership, asset=asset, work_order=work_order
        )
        _add_question(
            conversation=conversation,
            membership=membership,
            text=text,
            photo=photo,
            context=context,
            context_sources=context_sources,
        )
    return conversation


def ask(*, membership, conversation, text: str = "", photo=None, client_uuid=None) -> AssistantMessage:
    """Agrega una pregunta a la conversación; devuelve el mensaje del asistente en preparación.

    Idempotente por `client_uuid` (de la pregunta). No admite una pregunta mientras otra espera.
    """
    if client_uuid:
        existing = AssistantMessage.objects.filter(client_uuid=client_uuid).first()
        if existing is not None:
            _require_owner(membership, existing.conversation)
            return AssistantMessage.objects.get(conversation=existing.conversation, seq=existing.seq + 1)
    _require_access(membership)
    _require_owner(membership, conversation)
    _require_configured()
    text = _clean_text(text)
    if not text and photo is None:
        raise AssistantError(_("Escribe tu pregunta o agrega una foto."))
    with transaction.atomic():
        conversation = AssistantConversation.objects.select_for_update().get(pk=conversation.pk)
        if conversation.status != AssistantConversation.Status.OPEN:
            raise AssistantError(_("La conversación está cerrada."))
        if AssistantMessage.objects.filter(conversation=conversation, status=Status.PENDING).exists():
            raise AssistantError(_("Espera la respuesta anterior antes de preguntar de nuevo."))
        return _add_question(
            conversation=conversation, membership=membership, text=text, photo=photo, client_uuid=client_uuid
        )


def _next_seq(conversation) -> int:
    last = AssistantMessage.objects.filter(conversation=conversation).aggregate(last=Max("seq"))["last"]
    return (last or 0) + 1


def _add_question(
    *, conversation, membership, text, photo=None, context=None, context_sources=None, client_uuid=None
) -> AssistantMessage:
    """Pregunta (con el contexto del activo si es la primera) + respuesta pendiente; programa el turno.

    El contenido de la pregunta se guarda una vez y se reenvía sin cambios en cada turno siguiente.
    """
    if photo is not None and photo.content_type not in IMAGE_CONTENT_TYPES:
        raise AssistantError(_("La foto debe ser una imagen (JPEG, PNG o WebP)."))
    seq = _next_seq(conversation)
    question = AssistantMessage.objects.create(
        conversation=conversation,
        role=Role.USER,
        seq=seq,
        text=text,
        sources=context_sources or [],
        client_uuid=client_uuid,
        completed_at=timezone.now(),
    )
    content = []
    if context:
        content.append({"type": "text", "text": context, "cache_control": llm.EPHEMERAL})
    if photo is not None:
        attachment = add_attachment(
            target_type=TargetType.ASSISTANT_MESSAGE,
            target_id=question.pk,
            uploaded_file=photo,
            by=membership.user,
            purpose=Purpose.PHOTO,
        )
        content.append({"type": "galbust_photo", "attachment_id": str(attachment.pk)})
    if text:
        content.append({"type": "text", "text": text})
    question.content = content
    question.save(update_fields=["content", "updated_at"])
    answer = AssistantMessage.objects.create(
        conversation=conversation,
        role=Role.ASSISTANT,
        seq=seq + 1,
        status=Status.PENDING,
        model=conversation.model,
    )
    schedule_turn(message=answer)
    return answer


def propose_summary(*, membership, conversation) -> AssistantMessage:
    """Pide la nota de cierre propuesta (AI-04 CA1): otra llamada, mismo mecanismo pendiente/consulta."""
    _require_access(membership)
    _require_owner(membership, conversation)
    _require_configured()
    with transaction.atomic():
        conversation = AssistantConversation.objects.select_for_update().get(pk=conversation.pk)
        if AssistantMessage.objects.filter(conversation=conversation, status=Status.PENDING).exists():
            raise AssistantError(_("Espera la respuesta anterior antes de pedir la nota."))
        answered = AssistantMessage.objects.filter(
            conversation=conversation, kind=Kind.TURN, role=Role.ASSISTANT, status=Status.DONE
        ).exists()
        if not answered:
            raise AssistantError(_("Aún no hay respuestas para resumir."))
        message = AssistantMessage.objects.create(
            conversation=conversation,
            role=Role.ASSISTANT,
            kind=Kind.SUMMARY,
            seq=_next_seq(conversation),
            status=Status.PENDING,
            model=conversation.model,
        )
        conversation.summary_status = AssistantConversation.SummaryStatus.PENDING
        conversation.save(update_fields=["summary_status", "updated_at"])
        schedule_turn(message=message)
    return message


def mark_helpful(*, membership, conversation, helpful: str) -> AssistantConversation:
    """¿Te sirvió? sí / en parte / no (AI-04 CA2). Se puede cambiar; queda en la auditoría."""
    _require_access(membership)
    _require_owner(membership, conversation)
    if helpful not in AssistantConversation.Helpful.values:
        raise AssistantError(_("Elige sí, en parte o no."))
    with transaction.atomic():
        before = conversation.helpful
        conversation.helpful = helpful
        conversation.helpful_marked_at = timezone.now()
        conversation.save(update_fields=["helpful", "helpful_marked_at", "updated_at"])
        audit.record(
            action=AuditEvent.Action.UPDATE,
            target=conversation,
            membership=membership,
            changes={"helpful": [before, helpful]},
        )
    return conversation


def expire_stale_messages(*, conversation, now=None) -> int:
    """Pasa a error los mensajes pendientes hace más de `STALE_AFTER` (worker caído o reiniciado)."""
    limit = (now or timezone.now()) - STALE_AFTER
    with transaction.atomic():
        stale = list(
            AssistantMessage.objects.select_for_update().filter(
                conversation=conversation, status=Status.PENDING, created_at__lt=limit
            )
        )
        for message in stale:
            _finish_failed(message, status=Status.ERROR, code="timeout")
    return len(stale)


# --- Ejecución del turno ---------------------------------------------------------------------


def schedule_turn(*, message: AssistantMessage) -> None:
    """Corre el turno al confirmar la transacción: en el worker o en la misma petición."""
    organization = require_current_organization()
    organization_id, message_id = str(organization.pk), str(message.pk)
    if settings.AI_ASSISTANT_USE_QUEUE:
        from .tasks import run_turn as task

        transaction.on_commit(lambda: task.defer(organization_id=organization_id, message_id=message_id))
    else:

        def run_now():
            with tenant_context(organization):
                pending = AssistantMessage.objects.filter(pk=message_id).first()
                if pending is not None:
                    run_turn(message=pending)

        transaction.on_commit(run_now)


def _author_membership(conversation):
    membership = (
        Membership.objects.select_related("user")
        .filter(
            user_id=conversation.started_by_id, organization_id=conversation.organization_id, is_active=True
        )
        .first()
    )
    if membership is None or not has_perm(membership, Perm.ASSISTANT_USE):
        return None
    return membership


def _expand(block: dict) -> dict:
    """La foto se guarda como referencia al adjunto y se envía como imagen (siempre igual)."""
    if block.get("type") != "galbust_photo":
        return block
    attachment = Attachment.objects.filter(pk=block.get("attachment_id")).first()
    if attachment is None:
        return {"type": "text", "text": "(Foto no disponible)"}
    with attachment.file.open("rb") as handle:
        data = base64.standard_b64encode(handle.read()).decode("ascii")
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": attachment.content_type, "data": data},
    }


def build_history(*, conversation, before_seq: int) -> list[dict]:
    """Mensajes de la API de los turnos anteriores, sin editar (append-only).

    Preguntas: sus bloques guardados. Respuestas terminadas: todos los mensajes del turno
    (razonamiento, herramientas y resultados) tal como se recibieron y enviaron. Las respuestas
    con error o rechazadas y las notas de cierre no entran.
    """
    history = []
    messages = AssistantMessage.objects.filter(conversation=conversation, kind=Kind.TURN, seq__lt=before_seq)
    for message in messages.order_by("seq"):
        if message.role == Role.USER:
            if message.content:
                history.append({"role": "user", "content": [_expand(block) for block in message.content]})
        elif message.status == Status.DONE:
            history.extend(message.content)
    return history


def allowed_sources(*, conversation) -> dict:
    """{id: fuente} que se pueden citar: contexto del activo y resultados de herramientas."""
    allowed = {}
    for role, sources, tool_sources in AssistantMessage.objects.filter(conversation=conversation).values_list(
        "role", "sources", "tool_sources"
    ):
        for source in (sources if role == Role.USER else []) + (tool_sources or []):
            allowed.setdefault(source["id"], source)
    return allowed


@dataclass
class TurnResult:
    status: str
    error_code: str = ""
    content: list = field(default_factory=list)
    text: str = ""
    tool_sources: dict = field(default_factory=dict)
    usage: dict = field(default_factory=lambda: dict.fromkeys(USAGE_FIELDS, 0))
    tool_calls: int = 0
    model: str = ""


USAGE_FIELDS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens")


def _call_model(*, client, conversation, membership, history: list) -> TurnResult:
    """Bucle manual de herramientas. Suma los tokens de todas las rondas, también si falla."""
    max_rounds = settings.AI_ASSISTANT_MAX_TOOL_ROUNDS
    result = TurnResult(status=Status.DONE, model=conversation.model)
    turn: list = []
    tool_rounds = 0
    for _request in range(max_rounds + 2):
        params = llm.request_params(model=conversation.model, messages=history + turn, tools=tools.TOOLS)
        try:
            response = llm.create(client, params)
        except llm.api_errors() as error:
            logger.warning("Error de la API del asistente (%s)", type(error).__name__)
            result.status, result.error_code = Status.ERROR, llm.error_code(error)
            return result
        for key, value in llm.usage(response).items():
            result.usage[key] += value
        result.model = response.model or result.model
        if response.stop_reason == "refusal":
            result.status, result.error_code = Status.REFUSED, "refused"
            return result
        if response.stop_reason == "max_tokens":
            result.status, result.error_code = Status.ERROR, "max_tokens"
            return result
        turn.append({"role": "assistant", "content": llm.content_dicts(response)})
        tool_uses = [block for block in response.content if block.type == "tool_use"]
        if response.stop_reason == "pause_turn":
            continue
        if not tool_uses:
            result.content = turn
            result.text = "\n\n".join(
                block.text for block in response.content if block.type == "text"
            ).strip()
            return result
        results = []
        for block in tool_uses:
            if tool_rounds >= max_rounds:
                outcome = tools.ToolResult(tools._json({"error": LIMIT_REACHED}), is_error=True)
            else:
                outcome = tools.run_tool(
                    name=block.name, tool_input=block.input, membership=membership, conversation=conversation
                )
                result.tool_calls += 1
                for source in outcome.sources:
                    result.tool_sources.setdefault(source["id"], source)
            tool_result = {"type": "tool_result", "tool_use_id": block.id, "content": outcome.content}
            if outcome.is_error:
                tool_result["is_error"] = True
            results.append(tool_result)
        tool_rounds += 1
        turn.append({"role": "user", "content": results})
    result.status, result.error_code = Status.ERROR, "too_many_tools"
    return result


def _finish_failed(message, *, status, code, result: TurnResult | None = None) -> None:
    message.status = status
    message.error_code = code
    message.content = []
    message.completed_at = timezone.now()
    fields = ["status", "error_code", "content", "completed_at", "updated_at"]
    if result is not None:
        for key in USAGE_FIELDS:
            setattr(message, key, result.usage[key])
        message.tool_calls = result.tool_calls
        message.model = result.model
        fields += [*USAGE_FIELDS, "tool_calls", "model"]
    message.save(update_fields=fields)
    if message.kind == Kind.SUMMARY:
        AssistantConversation.objects.filter(pk=message.conversation_id).update(
            summary_status=AssistantConversation.SummaryStatus.ERROR, updated_at=timezone.now()
        )


def run_turn(*, message: AssistantMessage) -> AssistantMessage:
    """Responde un mensaje pendiente (turno o nota de cierre). Nunca deja el mensaje pendiente.

    Corre dentro del `tenant_context` de la organización (tarea o petición).
    """
    message = AssistantMessage.objects.select_related("conversation__asset").get(pk=message.pk)
    if message.status != Status.PENDING:
        return message
    conversation = message.conversation
    try:
        membership = _author_membership(conversation)
        if membership is None:
            code = "no_access"
        elif not module_enabled():
            code = "module"
        elif not llm.is_configured():
            code = "not_configured"
        else:
            code = ""
        if code:
            result = TurnResult(status=Status.ERROR, error_code=code)
        else:
            history = build_history(conversation=conversation, before_seq=message.seq)
            if message.kind == Kind.SUMMARY:
                history.append(
                    {"role": "user", "content": [{"type": "text", "text": llm.SUMMARY_INSTRUCTION}]}
                )
            result = _call_model(
                client=llm.get_client(), conversation=conversation, membership=membership, history=history
            )
    except Exception:
        logger.exception("Falló el turno %s del asistente", message.pk)
        result = TurnResult(status=Status.ERROR, error_code="internal")

    with transaction.atomic():
        message = AssistantMessage.objects.select_for_update().get(pk=message.pk)
        if message.status != Status.PENDING:
            return message  # otro proceso ya lo resolvió (p. ej. lo venció `expire_stale_messages`)
        if result.status != Status.DONE:
            _finish_failed(message, status=result.status, code=result.error_code, result=result)
            return message
        if message.kind == Kind.SUMMARY:
            text, _cited = validate_citations(result.text, {})
            conversation.summary = text
            conversation.summary_status = AssistantConversation.SummaryStatus.DONE
            conversation.save(update_fields=["summary", "summary_status", "updated_at"])
            message.text, message.sources = text, []
        else:
            allowed = allowed_sources(conversation=conversation) | result.tool_sources
            message.text, message.sources = validate_citations(result.text, allowed)
            message.tool_sources = list(result.tool_sources.values())
        message.content = result.content
        message.status = Status.DONE
        message.error_code = ""
        message.model = result.model
        message.tool_calls = result.tool_calls
        for key in USAGE_FIELDS:
            setattr(message, key, result.usage[key])
        message.completed_at = timezone.now()
        message.save()
    return message
