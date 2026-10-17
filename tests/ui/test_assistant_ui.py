"""Asistente en el teléfono (AI-01 CA4, ADR-017): texto ≥ 16 px, objetivos ≥ 48 px, cámara y "Más"."""

import re
from pathlib import Path

import pytest
from django.urls import reverse

from apps.assistant import services
from apps.core.tenancy import tenant_context
from tests.assistant_utils import install
from tests.conftest import login

CSS = (Path(__file__).resolve().parents[2] / "static" / "css" / "components.css").read_text(encoding="utf-8")


def _rule(selector: str) -> str:
    match = re.search(rf"(?m)^{re.escape(selector)} \{{([^}}]*)\}}", CSS)
    assert match, f"Falta la regla {selector}"
    return match.group(1)


@pytest.mark.parametrize("selector", [".chat__text", ".chat__status", ".source"])
def test_conversation_text_is_at_least_16px(selector):
    assert "font-size: var(--text-md)" in _rule(selector)


def test_sources_are_touch_targets():
    assert "min-height: var(--touch-target)" in _rule(".source")


@pytest.fixture
def conversation(settings, monkeypatch, tenant_a):
    settings.ANTHROPIC_API_KEY = "test-key"
    settings.AI_ASSISTANT_USE_QUEUE = False
    install(monkeypatch)
    with tenant_context(tenant_a.org):
        return services.start_conversation(membership=tenant_a.tech, asset=tenant_a.asset, text="Ruido")


def test_assistant_pages_live_under_more_in_both_navs(client, tenant_a, conversation):
    login(client, tenant_a.tech)
    body = client.get(reverse("assistant_conversation", args=[conversation.pk])).text
    more = reverse("menu")
    assert re.search(rf'<a href="{more}" aria-current="page">', body), "Más no queda marcado en la barra"
    assert re.search(rf'<a href="{more}" aria-current="page">[^<]*<svg', body), "ni en la navegación móvil"
    assert reverse("assistant_list") in client.get(more).text


def test_question_uses_dictation_and_the_phone_camera(client, tenant_a, conversation):
    from apps.assistant.models import AssistantMessage

    with tenant_context(tenant_a.org):
        AssistantMessage.objects.filter(conversation=conversation).update(status="done")
    login(client, tenant_a.tech)
    body = client.get(reverse("assistant_conversation", args=[conversation.pk])).text
    assert 'autocapitalize="sentences"' in body and 'spellcheck="true"' in body
    assert 'accept="image/*"' in body and 'capture="environment"' in body
    assert 'enctype="multipart/form-data"' in body
