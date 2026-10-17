"""Servicios del asistente de diagnóstico (AI-01..05, ADR-018) con un cliente falso de la API."""

import datetime
import json

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

from apps.assistant import llm, selectors, services, tasks
from apps.assistant.citations import segments, validate_citations
from apps.assistant.models import AssistantConversation, AssistantMessage
from apps.attachments.models import Attachment, TargetType
from apps.audit.models import AuditEvent
from apps.organizations.models import Plan
from tests.assistant_utils import (
    connection_error,
    install,
    rate_limit_error,
    reply,
    server_error,
    text,
    thinking,
    tool_use,
)
from tests.conftest import tiny_jpeg_bytes
from tests.inventory_utils import make_work_order

Status = AssistantMessage.Status


@pytest.fixture(autouse=True)
def configured(settings):
    settings.ANTHROPIC_API_KEY = "test-key"
    settings.AI_ASSISTANT_USE_QUEUE = False
    settings.AI_ASSISTANT_MAX_TOOL_ROUNDS = 6


@pytest.fixture
def bearing_order(in_a):
    order = make_work_order(in_a, title="Ruido en rodamiento", description="Vibración alta en el motor")
    order.completion_notes = "Se cambió el rodamiento lado acople"
    order.save(update_fields=["completion_notes"])
    return order


def _start(tenant, run, text_="Hace ruido el rodamiento", **kwargs):
    with run(execute=True):
        return services.start_conversation(membership=tenant.tech, asset=tenant.asset, text=text_, **kwargs)


def _messages(conversation):
    return list(AssistantMessage.objects.filter(conversation=conversation).order_by("seq"))


def test_turn_with_tool_round_saves_answer_tokens_and_valid_sources(
    in_a, bearing_order, monkeypatch, django_capture_on_commit_callbacks
):
    client = install(
        monkeypatch,
        reply(
            thinking("s1"), tool_use("search_work_orders", {"query": "rodamiento ruido", "asset_only": True})
        ),
        reply(
            thinking("s2"),
            text(f"Revisa el rodamiento lado acople [{bearing_order.code}].\n\nY la OT [OT-C-99999]."),
            input_tokens=300,
            output_tokens=50,
            cache_read=200,
            cache_write=40,
        ),
    )
    conversation = _start(in_a, django_capture_on_commit_callbacks)

    question, answer = _messages(conversation)
    assert question.role == "user" and question.content[0]["text"].startswith("<contexto_del_activo>")
    assert question.content[0]["cache_control"] == {"type": "ephemeral"}
    assert question.content[-1] == {"type": "text", "text": "Hace ruido el rodamiento"}
    assert answer.status == Status.DONE and answer.tool_calls == 1
    # Tokens sumados de las dos rondas.
    assert (answer.input_tokens, answer.output_tokens) == (400, 70)
    assert (answer.cache_read_tokens, answer.cache_creation_tokens) == (200, 40)
    # El marcador inventado se descarta; el real queda enlazado.
    assert [source["id"] for source in answer.sources] == [bearing_order.code]
    assert "OT-C-99999" not in answer.text and f"[{bearing_order.code}]" in answer.text
    assert [segment.is_general for segment in segments(answer.text, answer.sources)] == [False, True]
    # Turno completo guardado: assistant(tool_use) → user(tool_result) → assistant(final).
    assert [m["role"] for m in answer.content] == ["assistant", "user", "assistant"]
    tool_result = answer.content[1]["content"][0]
    assert tool_result["type"] == "tool_result" and bearing_order.code in tool_result["content"]

    first = client.calls[0]
    assert first["model"] == "claude-opus-5-5"
    assert first["output_config"] == {"effort": "medium"}
    assert first["betas"] == [llm.FALLBACK_BETA] and first["fallbacks"] == "default"
    assert first["tool_choice"] == {"type": "auto"} and "thinking" not in first
    assert [tool["name"] for tool in first["tools"]] == sorted(tool["name"] for tool in first["tools"])
    assert all(tool["strict"] for tool in first["tools"])
    assert first["system"][0]["cache_control"] == {"type": "ephemeral"}


def test_history_is_resent_append_only_without_edits(
    in_a, bearing_order, monkeypatch, django_capture_on_commit_callbacks
):
    client = install(
        monkeypatch,
        reply(thinking("s1"), tool_use("search_manuals", {"query": "rodamiento"})),
        reply(thinking("s2"), text("Primera respuesta.")),
        reply(thinking("s3"), text("Segunda respuesta.")),
    )
    conversation = _start(in_a, django_capture_on_commit_callbacks)
    with django_capture_on_commit_callbacks(execute=True):
        services.ask(membership=in_a.tech, conversation=conversation, text="¿Y la grasa?")

    first_turn_last_request, second_turn = client.calls[1]["messages"], client.calls[2]["messages"]
    first_answer = AssistantMessage.objects.get(conversation=conversation, seq=2)
    # El segundo turno empieza exactamente con lo enviado y recibido en el primero.
    assert second_turn[: len(first_turn_last_request)] == first_turn_last_request
    assert second_turn[len(first_turn_last_request)] == first_answer.content[-1]
    assert second_turn[-1] == {"role": "user", "content": [{"type": "text", "text": "¿Y la grasa?"}]}
    assert second_turn[1]["content"][0] == {"type": "thinking", "thinking": "", "signature": "s1"}
    # El sistema y las herramientas no cambian entre turnos (caché y razonamiento preservado).
    assert client.calls[0]["system"] == client.calls[2]["system"]
    assert client.calls[0]["tools"] == client.calls[2]["tools"]


@pytest.mark.parametrize(
    ("outcome", "status", "code"),
    [
        (reply(text(""), stop_reason="refusal"), Status.REFUSED, "refused"),
        (reply(text("Corta"), stop_reason="max_tokens"), Status.ERROR, "max_tokens"),
        (rate_limit_error(), Status.ERROR, "rate_limited"),
        (server_error(), Status.ERROR, "overloaded"),
        (connection_error(), Status.ERROR, "connection"),
    ],
)
def test_failed_turns_are_saved_without_content_and_left_out_of_history(
    in_a, monkeypatch, django_capture_on_commit_callbacks, outcome, status, code
):
    client = install(monkeypatch, outcome, reply(text("Ahora sí.")))
    conversation = _start(in_a, django_capture_on_commit_callbacks)
    answer = AssistantMessage.objects.get(conversation=conversation, seq=2)
    assert (answer.status, answer.error_code, answer.content) == (status, code, [])
    assert code in services.ERROR_MESSAGES
    assert not selectors.has_pending(conversation=conversation)

    with django_capture_on_commit_callbacks(execute=True):
        services.ask(membership=in_a.tech, conversation=conversation, text="Otra vez")
    # Dos preguntas seguidas; la respuesta fallida no se reenvía.
    assert [m["role"] for m in client.calls[1]["messages"]] == ["user", "user"]
    assert AssistantMessage.objects.get(conversation=conversation, seq=4).status == Status.DONE


def test_tool_rounds_are_capped(in_a, settings, monkeypatch, django_capture_on_commit_callbacks):
    settings.AI_ASSISTANT_MAX_TOOL_ROUNDS = 2
    looping = [reply(tool_use("search_manuals", {"query": "x"}, tool_id=f"t{i}")) for i in range(4)]
    client = install(monkeypatch, *looping)
    conversation = _start(in_a, django_capture_on_commit_callbacks)
    answer = AssistantMessage.objects.get(conversation=conversation, seq=2)
    assert (answer.status, answer.error_code, answer.tool_calls) == (Status.ERROR, "too_many_tools", 2)
    assert len(client.calls) == 4
    limit = client.calls[3]["messages"][-1]["content"][0]
    assert limit["is_error"] is True and "Límite" in limit["content"]


def test_invalid_tool_input_returns_an_error_result(in_a, monkeypatch, django_capture_on_commit_callbacks):
    client = install(
        monkeypatch,
        reply(tool_use("asset_failure_history", {"asset_code": "NO-EXISTE"})),
        reply(text("No encontré ese equipo.")),
    )
    _start(in_a, django_capture_on_commit_callbacks)
    result = client.calls[1]["messages"][-1]["content"][0]
    assert result["is_error"] is True


def test_not_configured_warns_instead_of_failing(
    in_a, settings, monkeypatch, django_capture_on_commit_callbacks
):
    settings.ANTHROPIC_API_KEY = ""
    assert not services.is_configured()
    with pytest.raises(services.AssistantNotConfiguredError):
        services.start_conversation(membership=in_a.tech, asset=in_a.asset, text="Ruido")
    assert not AssistantConversation.objects.exists()

    # Un turno pendiente sin clave (p. ej. se quitó después) termina en error, no queda pendiente.
    settings.ANTHROPIC_API_KEY = "test-key"
    install(monkeypatch)
    with django_capture_on_commit_callbacks(execute=False):
        conversation = services.start_conversation(membership=in_a.tech, asset=in_a.asset, text="Ruido")
    settings.ANTHROPIC_API_KEY = ""
    answer = services.run_turn(message=AssistantMessage.objects.get(conversation=conversation, seq=2))
    assert (answer.status, answer.error_code) == (Status.ERROR, "not_configured")


def test_get_client_without_key_raises(settings):
    settings.ANTHROPIC_API_KEY = ""
    with pytest.raises(llm.NotConfigured):
        llm.get_client()


def test_monthly_quota_counts_conversations_of_the_calendar_month(in_a, monkeypatch):
    install(monkeypatch)
    plan = Plan.objects.get(code="pro")
    plan.max_assistant_conversations = 1
    plan.save(update_fields=["max_assistant_conversations"])
    in_a.org.plan = plan
    in_a.org.save(update_fields=["plan"])

    first = services.start_conversation(membership=in_a.tech, asset=in_a.asset, text="Ruido")
    assert selectors.quota_status(organization=in_a.org).exhausted
    with pytest.raises(services.AssistantQuotaExceededError):
        services.start_conversation(membership=in_a.tech, asset=in_a.asset, text="Otra")
    # Preguntar en una conversación ya abierta no consume cupo.
    AssistantMessage.objects.filter(conversation=first).update(status=Status.DONE)
    services.ask(membership=in_a.tech, conversation=first, text="¿Y ahora?")

    # Las del mes anterior (en la zona de la organización) no cuentan.
    last_month = selectors.month_start(in_a.org) - datetime.timedelta(minutes=1)
    AssistantConversation.objects.filter(pk=first.pk).update(created_at=last_month)
    assert selectors.quota_status(organization=in_a.org).used == 0
    services.start_conversation(membership=in_a.tech, asset=in_a.asset, text="Nueva")


def test_month_start_uses_the_organization_timezone(in_a):
    # 2026-11-01 02:00 UTC es todavía octubre en Santiago (UTC-3).
    now = datetime.datetime(2026, 11, 1, 2, 0, tzinfo=datetime.UTC)
    start = selectors.month_start(in_a.org, now)
    assert (start.month, start.day) == (10, 1)


def test_no_plan_means_no_quota(in_a):
    status = selectors.quota_status(organization=in_a.org)
    assert status.limit is None and status.remaining is None and not status.exhausted


def test_summary_is_a_separate_call_left_out_of_history(
    in_a, monkeypatch, django_capture_on_commit_callbacks
):
    client = install(
        monkeypatch,
        reply(text("Cambia el rodamiento.")),
        reply(text("Síntoma: ruido. Causa: rodamiento gastado. Acción: cambio [OT-C-99999].")),
        reply(text("Listo.")),
    )
    conversation = _start(in_a, django_capture_on_commit_callbacks)
    with django_capture_on_commit_callbacks(execute=True):
        services.propose_summary(membership=in_a.tech, conversation=conversation)
    conversation.refresh_from_db()
    assert conversation.summary_status == AssistantConversation.SummaryStatus.DONE
    assert conversation.summary == "Síntoma: ruido. Causa: rodamiento gastado. Acción: cambio."
    assert client.calls[1]["messages"][-1]["content"][0]["text"] == llm.SUMMARY_INSTRUCTION

    with django_capture_on_commit_callbacks(execute=True):
        services.ask(membership=in_a.tech, conversation=conversation, text="Gracias")
    sent = client.calls[2]["messages"]
    assert all(llm.SUMMARY_INSTRUCTION not in json.dumps(m, ensure_ascii=False) for m in sent)
    assert [m.kind for m in selectors.conversation_messages(conversation=conversation)].count("summary") == 0


def test_summary_requires_an_answer(in_a, monkeypatch):
    install(monkeypatch)
    conversation = services.start_conversation(membership=in_a.tech, asset=in_a.asset, text="Ruido")
    with pytest.raises(services.AssistantError):
        services.propose_summary(membership=in_a.tech, conversation=conversation)


def test_failed_summary_marks_the_conversation(in_a, monkeypatch, django_capture_on_commit_callbacks):
    install(monkeypatch, reply(text("Respuesta.")), rate_limit_error())
    conversation = _start(in_a, django_capture_on_commit_callbacks)
    with django_capture_on_commit_callbacks(execute=True):
        services.propose_summary(membership=in_a.tech, conversation=conversation)
    conversation.refresh_from_db()
    assert conversation.summary_status == AssistantConversation.SummaryStatus.ERROR


def test_mark_helpful_is_audited(in_a, monkeypatch):
    install(monkeypatch)
    conversation = services.start_conversation(membership=in_a.tech, asset=in_a.asset, text="Ruido")
    services.mark_helpful(membership=in_a.tech, conversation=conversation, helpful="partial")
    conversation.refresh_from_db()
    assert conversation.helpful == "partial" and conversation.helpful_marked_at
    event = AuditEvent.objects.filter(action=AuditEvent.Action.UPDATE).latest("created_at")
    assert event.changes == {"helpful": ["", "partial"]}
    with pytest.raises(services.AssistantError):
        services.mark_helpful(membership=in_a.tech, conversation=conversation, helpful="maybe")


def test_start_is_audited_and_titled(in_a, monkeypatch):
    install(monkeypatch)
    conversation = services.start_conversation(
        membership=in_a.tech, asset=in_a.asset, text="  Alarma E42 en el panel\nDesde ayer"
    )
    assert conversation.title == "Alarma E42 en el panel"
    assert conversation.model == "claude-opus-5-5" and conversation.started_by == in_a.tech.user
    assert AuditEvent.objects.filter(action=AuditEvent.Action.CREATE, target_id=str(conversation.pk)).exists()


def test_work_order_must_belong_to_the_asset(in_a, monkeypatch):
    install(monkeypatch)
    from apps.assets.models import Asset

    other = Asset.objects.create(code="CMP-02", name="Compresor 2", asset_class=in_a.asset.asset_class)
    order = make_work_order(in_a, asset=other)
    with pytest.raises(services.AssistantError):
        services.start_conversation(membership=in_a.tech, asset=in_a.asset, work_order=order, text="x")
    conversation = services.start_conversation(membership=in_a.tech, asset=other, work_order=order, text="x")
    assert order.code in conversation.messages.get(seq=1).content[0]["text"]


def test_question_is_required_and_bounded(in_a, monkeypatch):
    install(monkeypatch)
    with pytest.raises(services.AssistantError):
        services.start_conversation(membership=in_a.tech, asset=in_a.asset, text="   ")
    with pytest.raises(services.AssistantError):
        services.start_conversation(membership=in_a.tech, asset=in_a.asset, text="x" * 4001)


def test_pending_answer_blocks_a_new_question(in_a, monkeypatch):
    install(monkeypatch)
    conversation = services.start_conversation(membership=in_a.tech, asset=in_a.asset, text="Ruido")
    assert selectors.has_pending(conversation=conversation)
    with pytest.raises(services.AssistantError):
        services.ask(membership=in_a.tech, conversation=conversation, text="¿Y?")


def test_closed_conversation_rejects_questions(in_a, monkeypatch):
    install(monkeypatch)
    conversation = services.start_conversation(membership=in_a.tech, asset=in_a.asset, text="Ruido")
    AssistantMessage.objects.filter(conversation=conversation).update(status=Status.DONE)
    AssistantConversation.objects.filter(pk=conversation.pk).update(status="closed")
    with pytest.raises(services.AssistantError):
        services.ask(membership=in_a.tech, conversation=conversation, text="¿Y?")


def test_client_uuid_makes_start_and_ask_idempotent(in_a, monkeypatch):
    import uuid

    install(monkeypatch)
    key = uuid.uuid4()
    first = services.start_conversation(membership=in_a.tech, asset=in_a.asset, text="Ruido", client_uuid=key)
    again = services.start_conversation(membership=in_a.tech, asset=in_a.asset, text="Ruido", client_uuid=key)
    assert first == again and AssistantConversation.objects.count() == 1

    AssistantMessage.objects.filter(conversation=first).update(status=Status.DONE)
    question_key = uuid.uuid4()
    answer = services.ask(membership=in_a.tech, conversation=first, text="¿Y?", client_uuid=question_key)
    repeated = services.ask(membership=in_a.tech, conversation=first, text="¿Y?", client_uuid=question_key)
    assert answer == repeated and AssistantMessage.objects.filter(conversation=first).count() == 4


def test_photo_is_attached_and_sent_as_image(in_a, monkeypatch, django_capture_on_commit_callbacks):
    client = install(monkeypatch, reply(text("Se ve la placa.")))
    photo = SimpleUploadedFile("placa.jpg", tiny_jpeg_bytes(), content_type="image/jpeg")
    conversation = _start(in_a, django_capture_on_commit_callbacks, text_="", photo=photo)
    question = conversation.messages.get(seq=1)
    attachment = Attachment.objects.get()
    assert (attachment.target_type, attachment.target_id) == (TargetType.ASSISTANT_MESSAGE, question.pk)
    assert question.content[1] == {"type": "galbust_photo", "attachment_id": str(attachment.pk)}
    sent = client.calls[0]["messages"][0]["content"][1]
    assert sent["type"] == "image" and sent["source"]["media_type"] == "image/jpeg" and sent["source"]["data"]
    assert selectors.message_photos(messages=[question]) == {question.pk: [attachment]}
    assert conversation.title == f"Consulta sobre {in_a.asset.code}"


def test_photo_must_be_an_image(in_a, monkeypatch):
    install(monkeypatch)
    pdf = SimpleUploadedFile("x.pdf", b"%PDF-1.4", content_type="application/pdf")
    with pytest.raises(services.AssistantError):
        services.start_conversation(membership=in_a.tech, asset=in_a.asset, text="x", photo=pdf)


def test_stale_pending_messages_expire(in_a, monkeypatch):
    install(monkeypatch)
    conversation = services.start_conversation(membership=in_a.tech, asset=in_a.asset, text="Ruido")
    assert services.expire_stale_messages(conversation=conversation) == 0
    later = (
        AssistantMessage.objects.get(seq=2).created_at + services.STALE_AFTER + datetime.timedelta(seconds=1)
    )
    assert services.expire_stale_messages(conversation=conversation, now=later) == 1
    answer = AssistantMessage.objects.get(seq=2)
    assert (answer.status, answer.error_code) == (Status.ERROR, "timeout")
    # Un turno que termina después no pisa el vencimiento.
    assert services.run_turn(message=answer).status == Status.ERROR


def test_queue_defers_the_turn_with_its_organization(
    in_a, settings, monkeypatch, django_capture_on_commit_callbacks
):
    settings.AI_ASSISTANT_USE_QUEUE = True
    deferred = []
    monkeypatch.setattr(tasks.run_turn, "defer", lambda **kw: deferred.append(kw))
    install(monkeypatch)
    conversation = _start(in_a, django_capture_on_commit_callbacks)
    answer = conversation.messages.get(seq=2)
    assert deferred == [{"organization_id": str(in_a.org.pk), "message_id": str(answer.pk)}]
    assert answer.status == Status.PENDING


def test_task_runs_the_turn_in_the_organization_context(tenant_a, monkeypatch):
    from apps.core.tenancy import tenant_context

    install(monkeypatch, reply(text("Hecho.")))
    with tenant_context(tenant_a.org):
        conversation = services.start_conversation(
            membership=tenant_a.tech, asset=tenant_a.asset, text="Ruido"
        )
        answer = conversation.messages.get(seq=2)
    assert tasks.run_turn(organization_id=str(tenant_a.org.pk), message_id=str(answer.pk)) == Status.DONE
    assert tasks.run_turn(organization_id=str(tenant_a.org.pk), message_id=str(answer.pk)) == Status.DONE


def test_unexpected_errors_never_leave_the_message_pending(
    in_a, monkeypatch, django_capture_on_commit_callbacks
):
    install(monkeypatch, RuntimeError("boom"))
    conversation = _start(in_a, django_capture_on_commit_callbacks)
    answer = conversation.messages.get(seq=2)
    assert (answer.status, answer.error_code) == (Status.ERROR, "internal")


def test_deactivated_author_gets_no_access(in_a, monkeypatch):
    install(monkeypatch)
    conversation = services.start_conversation(membership=in_a.tech, asset=in_a.asset, text="Ruido")
    in_a.tech.is_active = False
    in_a.tech.save(update_fields=["is_active"])
    answer = services.run_turn(message=conversation.messages.get(seq=2))
    assert (answer.status, answer.error_code) == (Status.ERROR, "no_access")


def test_validate_citations_keeps_only_allowed_ids():
    allowed = {"OT-C-00001": {"id": "OT-C-00001", "type": "work_order", "label": "OT"}}
    clean, cited = validate_citations(
        "Uno [OT-C-00001, OT-C-00002]. Dos [MAN:abcdef12:3]. Tres [nota]", allowed
    )
    assert clean == "Uno [OT-C-00001]. Dos. Tres [nota]"
    assert cited == [allowed["OT-C-00001"]]
