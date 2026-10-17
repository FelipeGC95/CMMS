"""Datos de uso del asistente con tokens conocidos, para la vista de costo (AI-05 CA2)."""

import datetime

from apps.assistant.models import AssistantConversation, AssistantMessage
from apps.core.tenancy import tenant_context

# USD por millón de tokens, fijos en los tests (no dependen de los precios de settings).
PRICES = {
    "claude-opus-5-5": {"input": 4.0, "output": 20.0, "cache_read": 0.20, "cache_write": 5.0},
    "claude-sonnet-5-5": {"input": 2.0, "output": 10.0, "cache_read": 0.20, "cache_write": 2.5},
}
NOW = datetime.datetime(2026, 10, 15, 12, 0, tzinfo=datetime.UTC)


def usage(tenant, *, at, model="claude-opus-5-5", role="assistant", **tokens):
    """Una conversación con una respuesta de `model` y esos tokens, fechada en `at`."""
    with tenant_context(tenant.org):
        conversation = AssistantConversation.objects.create(
            asset=tenant.asset, started_by=tenant.tech.user, title="Uso", model=model
        )
        message = AssistantMessage.objects.create(
            conversation=conversation, role=role, seq=1, status="done", model=model, **tokens
        )
        AssistantConversation.objects.filter(pk=conversation.pk).update(created_at=at)
        AssistantMessage.objects.filter(pk=message.pk).update(created_at=at)
    return message
