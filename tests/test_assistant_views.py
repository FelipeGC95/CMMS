"""Pantallas del asistente de diagnóstico (AI-01..05): pedir ayuda, conversación, lista y cierre."""

import uuid

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from apps.assistant import selectors, services
from apps.assistant.models import AssistantConversation, AssistantMessage
from apps.attachments.models import Attachment, TargetType
from apps.audit.models import AuditEvent
from apps.core.tenancy import tenant_context
from apps.organizations.models import Plan
from apps.work.models import WorkOrder
from tests.assistant_utils import answered_conversation, install, rate_limit_error, reply, text
from tests.conftest import login, tiny_jpeg_bytes
from tests.inventory_utils import make_work_order, set_status


@pytest.fixture(autouse=True)
def configured(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    settings.ANTHROPIC_API_KEY = "test-key"
    settings.AI_ASSISTANT_USE_QUEUE = False


def _start_url(asset, work_order=None):
    url = f"{reverse('assistant_start')}?asset={asset.pk}"
    return f"{url}&work_order={work_order.pk}" if work_order else url


def _conversation(tenant, **kwargs):
    with tenant_context(tenant.org):
        return services.start_conversation(
            membership=kwargs.pop("membership", tenant.tech), asset=tenant.asset, text="Ruido", **kwargs
        )


# --- "Pedir ayuda" en la ficha, por QR y en la OT (AI-01 CA1) ------------------------------


def test_asset_detail_and_qr_landing_offer_help(client, tenant_a):
    login(client, tenant_a.tech)
    start = _start_url(tenant_a.asset)
    assert start.replace("&", "&amp;") in client.get(reverse("asset_detail", args=[tenant_a.asset.pk])).text
    # Con sesión, el QR lleva a "Avisar falla": ahí también se ofrece pedir ayuda.
    response = client.get(reverse("asset_qr", args=[tenant_a.asset.qr_token]), follow=True)
    assert start in response.text


def test_help_is_hidden_without_permission_or_module(client, tenant_a):
    detail = reverse("asset_detail", args=[tenant_a.asset.pk])
    login(client, tenant_a.viewer)
    assert reverse("assistant_start") not in client.get(detail).text
    tenant_a.org.plan = Plan.objects.get(code="basic")
    tenant_a.org.save(update_fields=["plan"])
    login(client, tenant_a.tech)
    assert reverse("assistant_start") not in client.get(detail).text
    assert reverse("assistant_list") not in client.get(reverse("menu")).text


def test_help_is_shown_but_warns_when_not_configured(client, tenant_a, settings):
    settings.ANTHROPIC_API_KEY = ""
    login(client, tenant_a.tech)
    assert reverse("assistant_start") in client.get(reverse("asset_detail", args=[tenant_a.asset.pk])).text
    page = client.get(_start_url(tenant_a.asset))
    assert page.status_code == 200
    assert "El asistente no está configurado" in page.text and 'name="text"' not in page.text
    response = client.post(_start_url(tenant_a.asset), {"text": "Ruido", "client_uuid": uuid.uuid4()})
    assert response.status_code == 400 and "no está configurado" in response.text
    with tenant_context(tenant_a.org):
        assert not AssistantConversation.objects.exists()


def test_work_order_detail_offers_help_for_its_asset(client, tenant_a):
    with tenant_context(tenant_a.org):
        order = make_work_order(tenant_a)
    login(client, tenant_a.tech)
    body = client.get(reverse("work_order_detail", args=[order.pk])).text
    assert _start_url(tenant_a.asset, order).replace("&", "&amp;") in body


# --- Pedir ayuda y conversación (AI-01, AI-02, AI-03) --------------------------------------


def test_start_page_shows_safety_notice_quota_and_camera_input(client, tenant_a):
    tenant_a.org.plan = Plan.objects.get(code="pro")
    tenant_a.org.save(update_fields=["plan"])
    login(client, tenant_a.tech)
    body = client.get(_start_url(tenant_a.asset)).text
    assert "bloqueo y etiquetado" in body and "no reemplaza tu criterio" in body
    assert 'accept="image/*"' in body and 'capture="environment"' in body
    assert 'name="client_uuid"' in body
    assert "Quedan 100 conversaciones este mes." in body


def test_start_creates_the_conversation_and_shows_the_answer_with_sources(
    client, tenant_a, monkeypatch, django_capture_on_commit_callbacks
):
    with tenant_context(tenant_a.org):
        order = make_work_order(tenant_a, title="Ruido en rodamiento")
    install(monkeypatch, reply(text(f"Revisa el rodamiento [{order.code}].\n\nPuede ser desalineación.")))
    login(client, tenant_a.tech)
    client_uuid = uuid.uuid4()
    photo = SimpleUploadedFile("placa.jpg", tiny_jpeg_bytes(), content_type="image/jpeg")
    with django_capture_on_commit_callbacks(execute=True):
        response = client.post(
            _start_url(tenant_a.asset, order),
            {"text": "Hace ruido", "photo": photo, "client_uuid": client_uuid},
        )
    with tenant_context(tenant_a.org):
        conversation = AssistantConversation.objects.get()
    assert response.status_code == 302 and response.url == reverse(
        "assistant_conversation", args=[conversation.pk]
    )
    assert conversation.work_order_id == order.pk and conversation.client_uuid == client_uuid

    body = client.get(response.url).text
    assert "Revisa el rodamiento" in body
    assert reverse("work_order_detail", args=[order.pk]) in body and "Historial" in body
    assert "Conocimiento general (menor confianza)" in body
    assert "bloqueo y etiquetado" in body  # aviso de seguridad visible (AI-03 CA2)
    assert 'hx-trigger="every 2s"' not in body  # nada pendiente: no consulta
    with tenant_context(tenant_a.org):
        photo_pk = Attachment.objects.get(target_type=TargetType.ASSISTANT_MESSAGE).pk
    assert reverse("attachment", args=[photo_pk]) in body
    assert client.get(reverse("attachment", args=[photo_pk])).status_code == 200


def test_start_is_idempotent_by_client_uuid(client, tenant_a):
    login(client, tenant_a.tech)
    data = {"text": "Ruido", "client_uuid": uuid.uuid4()}
    first = client.post(_start_url(tenant_a.asset), data)
    second = client.post(_start_url(tenant_a.asset), data)
    assert first.url == second.url
    with tenant_context(tenant_a.org):
        assert AssistantConversation.objects.count() == 1


def test_start_requires_text_or_photo(client, tenant_a):
    login(client, tenant_a.tech)
    response = client.post(_start_url(tenant_a.asset), {"text": " ", "client_uuid": uuid.uuid4()})
    assert response.status_code == 400 and "Escribe tu pregunta o agrega una foto." in response.text


def test_exhausted_quota_is_explained_and_blocks_new_conversations(client, tenant_a):
    plan = Plan.objects.create(code="mini", name="Mini", features=[], max_assistant_conversations=1)
    tenant_a.org.plan = plan
    tenant_a.org.save(update_fields=["plan"])
    _conversation(tenant_a)
    login(client, tenant_a.tech)
    page = client.get(_start_url(tenant_a.asset))
    assert "Se usaron las 1 conversaciones" in page.text and 'name="text"' not in page.text
    response = client.post(_start_url(tenant_a.asset), {"text": "Otra", "client_uuid": uuid.uuid4()})
    assert response.status_code == 400
    with tenant_context(tenant_a.org):
        assert AssistantConversation.objects.count() == 1
    # El resto del sistema sigue igual (AI-05 CA1).
    assert client.get(reverse("asset_detail", args=[tenant_a.asset.pk])).status_code == 200


def test_pending_answer_polls_every_two_seconds_until_done(client, tenant_a):
    conversation = _conversation(tenant_a)  # en los tests on_commit no corre: queda pendiente
    login(client, tenant_a.tech)
    body = client.get(reverse("assistant_conversation", args=[conversation.pk])).text
    thread = reverse("assistant_thread", args=[conversation.pk])
    assert f'hx-get="{thread}"' in body and 'hx-trigger="every 2s"' in body
    assert "Pensando" in body and 'name="text"' not in body  # no se pregunta mientras espera

    with tenant_context(tenant_a.org):
        AssistantMessage.objects.filter(conversation=conversation, seq=2).update(status="done", text="Listo.")
    fragment = client.get(thread)
    assert fragment.status_code == 200 and "<html" not in fragment.text
    assert "hx-trigger" not in fragment.text and "Listo." in fragment.text and 'name="text"' in fragment.text


def test_failed_turn_shows_a_clear_message_and_the_conversation_stays_usable(
    client, tenant_a, monkeypatch, django_capture_on_commit_callbacks
):
    install(monkeypatch, rate_limit_error())
    login(client, tenant_a.tech)
    with django_capture_on_commit_callbacks(execute=True):
        response = client.post(_start_url(tenant_a.asset), {"text": "Ruido", "client_uuid": uuid.uuid4()})
    body = client.get(response.url).text
    assert str(services.ERROR_MESSAGES["rate_limited"]) in body
    assert 'name="text"' in body


def test_follow_up_question_summary_helpful_and_close(client, tenant_a, monkeypatch):
    conversation = answered_conversation(tenant_a, monkeypatch)
    url = reverse("assistant_conversation", args=[conversation.pk])
    login(client, tenant_a.tech)
    body = client.get(url).text
    assert "Proponer nota de cierre" in body and "¿Te sirvió la ayuda?" in body

    question = {"text": "Revisé y está bien", "client_uuid": uuid.uuid4()}
    response = client.post(reverse("assistant_ask", args=[conversation.pk]), question)
    assert response.status_code == 302 and response.url == f"{url}#composer"
    client.post(reverse("assistant_ask", args=[conversation.pk]), question)  # doble envío
    with tenant_context(tenant_a.org):
        assert AssistantMessage.objects.filter(conversation=conversation).count() == 4
        AssistantMessage.objects.filter(conversation=conversation).update(status="done")

    client.post(reverse("assistant_summary", args=[conversation.pk]))
    with tenant_context(tenant_a.org):
        conversation.refresh_from_db()
        assert conversation.summary_status == "pending"
    assert "Preparando la nota de cierre" in client.get(url).text

    client.post(reverse("assistant_helpful", args=[conversation.pk]), {"helpful": "partial"})
    response = client.post(reverse("assistant_close", args=[conversation.pk]))
    assert response.status_code == 302
    with tenant_context(tenant_a.org):
        conversation.refresh_from_db()
        assert (conversation.helpful, conversation.status) == ("partial", "closed")
    body = client.get(url).text
    assert 'name="text"' not in body and "Sirvió en parte" in body


def test_ask_errors_keep_the_question_on_screen(client, tenant_a):
    conversation = _conversation(tenant_a)  # con la respuesta anterior pendiente
    login(client, tenant_a.tech)
    response = client.post(
        reverse("assistant_ask", args=[conversation.pk]), {"text": "Otra cosa", "client_uuid": uuid.uuid4()}
    )
    assert response.status_code == 400 and "Espera la respuesta anterior" in response.text


def test_admin_reads_others_conversations_without_acting(client, tenant_a, monkeypatch):
    conversation = answered_conversation(tenant_a, monkeypatch)
    login(client, tenant_a.admin)
    body = client.get(reverse("assistant_conversation", args=[conversation.pk])).text
    assert "Revisa el rodamiento" in body and tenant_a.tech.user.full_name in body
    assert 'name="text"' not in body and "Proponer nota de cierre" not in body
    for name in ("assistant_ask", "assistant_summary", "assistant_close"):
        assert client.post(reverse(name, args=[conversation.pk]), {"text": "x"}).status_code == 403
    response = client.post(reverse("assistant_helpful", args=[conversation.pk]), {"helpful": "yes"})
    assert response.status_code == 403


# --- Lista (AI-05 CA3) ---------------------------------------------------------------------


def test_list_shows_own_conversations_and_all_for_the_admin(client, tenant_a):
    theirs = _conversation(tenant_a)
    mine = _conversation(tenant_a, membership=tenant_a.admin)
    login(client, tenant_a.admin)
    own = client.get(reverse("assistant_list")).text
    assert reverse("assistant_conversation", args=[mine.pk]) in own
    assert reverse("assistant_conversation", args=[theirs.pk]) not in own
    everything = client.get(reverse("assistant_list") + "?scope=all").text
    assert reverse("assistant_conversation", args=[theirs.pk]) in everything
    assert tenant_a.tech.user.full_name in everything

    login(client, tenant_a.tech)
    tech = client.get(reverse("assistant_list") + "?scope=all").text  # sin view_all: ignora el filtro
    assert reverse("assistant_conversation", args=[theirs.pk]) in tech
    assert reverse("assistant_conversation", args=[mine.pk]) not in tech
    assert "?scope=all" not in tech


def test_list_is_paginated(client, tenant_a):
    for i in range(26):
        with tenant_context(tenant_a.org):
            services.start_conversation(membership=tenant_a.tech, asset=tenant_a.asset, text=f"Ruido {i}")
    login(client, tenant_a.tech)
    body = client.get(reverse("assistant_list")).text
    assert "1 / 2" in body and body.count('data-label=""><a href="/assistant/') == 25


def test_menu_links_to_the_assistant(client, tenant_a):
    login(client, tenant_a.tech)
    assert reverse("assistant_list") in client.get(reverse("menu")).text


# --- Nota de cierre y "¿Te sirvió?" al completar la OT (AI-04) -------------------------------


def _ready_to_complete(tenant, monkeypatch):
    with tenant_context(tenant.org):
        order = make_work_order(tenant, title="Ruido en rodamiento", assigned_to=tenant.tech.user)
    conversation = answered_conversation(tenant, monkeypatch, work_order=order)
    with tenant_context(tenant.org):
        AssistantConversation.objects.filter(pk=conversation.pk).update(
            summary="Síntoma: ruido. Causa: rodamiento seco. Acción: reengrase.", summary_status="done"
        )
        set_status(order, WorkOrder.Status.IN_PROGRESS)
    return order, conversation


def test_complete_form_preloads_the_proposed_note_and_can_discard_it(client, tenant_a, monkeypatch):
    order, conversation = _ready_to_complete(tenant_a, monkeypatch)
    login(client, tenant_a.tech)
    url = reverse("work_order_detail", args=[order.pk])
    body = client.get(url).text
    assert "Causa: rodamiento seco. Acción: reengrase.</textarea>" in body
    assert "Descartar nota propuesta" in body and 'name="assistant_helpful"' in body
    assert reverse("assistant_conversation", args=[conversation.pk]) in body
    discarded = client.get(url + "?summary=discard").text
    assert "reengrase.</textarea>" not in discarded and "Descartar nota propuesta" not in discarded


def test_completing_with_the_note_saves_it_and_the_helpful_mark(client, tenant_a, monkeypatch):
    order, conversation = _ready_to_complete(tenant_a, monkeypatch)
    login(client, tenant_a.tech)
    response = client.post(
        reverse("work_order_complete", args=[order.pk]),
        {
            "symptom": "Ruido al partir",
            "notes": "Reengrase del rodamiento (nota editada)",
            "labor_minutes": 30,
            "impact": "partial",
            "assistant_helpful": "yes",
        },
    )
    assert response.status_code == 302
    with tenant_context(tenant_a.org):
        order.refresh_from_db()
        conversation.refresh_from_db()
        assert not order.is_open
        assert order.completion_notes == "Reengrase del rodamiento (nota editada)"
        assert conversation.helpful == "yes"
        changes = AuditEvent.objects.filter(target_id=conversation.pk).values_list("changes", flat=True)
        assert {"helpful": ["", "yes"]} in list(changes)


def test_complete_form_without_assistant_has_no_extra_fields(client, tenant_a):
    with tenant_context(tenant_a.org):
        order = set_status(make_work_order(tenant_a), WorkOrder.Status.IN_PROGRESS)
    login(client, tenant_a.tech)
    body = client.get(reverse("work_order_detail", args=[order.pk])).text
    assert 'name="assistant_helpful"' not in body and "Descartar nota propuesta" not in body


# --- Servicio de cierre de la conversación ---------------------------------------------------


def test_close_conversation_service(tenant_a):
    conversation = _conversation(tenant_a)
    with tenant_context(tenant_a.org):
        with pytest.raises(services.PermissionDeniedError):
            services.close_conversation(membership=tenant_a.admin, conversation=conversation)
        services.close_conversation(membership=tenant_a.tech, conversation=conversation)
        services.close_conversation(membership=tenant_a.tech, conversation=conversation)  # idempotente
        conversation.refresh_from_db()
        assert conversation.status == "closed"
        changes = AuditEvent.objects.filter(target_id=conversation.pk).values_list("changes", flat=True)
        assert list(changes).count({"status": ["open", "closed"]}) == 1
        AssistantMessage.objects.filter(conversation=conversation).update(status="done")
        with pytest.raises(services.AssistantError):
            services.ask(membership=tenant_a.tech, conversation=conversation, text="¿Y ahora?")
        assert selectors.conversation_for(membership=tenant_a.tech, pk=conversation.pk) == conversation
