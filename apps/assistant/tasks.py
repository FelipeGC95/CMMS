"""Tareas del asistente (Procrastinate, ADR-007): un turno de conversación (ADR-018).

Envoltorio delgado sobre `services.run_turn`. Solo se encola con `AI_ASSISTANT_USE_QUEUE`; si
no, el servicio corre el turno en la misma petición (ver `services.schedule_turn`). Sin
reintentos automáticos: un turno repetido cobraría tokens dos veces; el error queda en el
mensaje y el técnico vuelve a preguntar.
"""

from procrastinate.contrib.django import app

from apps.core.tenancy import tenant_context
from apps.organizations.models import Organization

from . import services
from .models import AssistantMessage


@app.task(name="assistant.run_turn", queue="assistant", retry=0)
def run_turn(*, organization_id: str, message_id: str) -> str:
    organization = Organization.objects.filter(pk=organization_id, is_active=True).first()
    if organization is None:
        return ""
    with tenant_context(organization):
        message = AssistantMessage.objects.filter(pk=message_id).first()
        if message is None:
            return ""
        return services.run_turn(message=message).status
