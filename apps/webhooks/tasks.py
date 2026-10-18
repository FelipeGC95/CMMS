"""Entrega de webhooks en el worker (Procrastinate, ADR-007/ADR-030).

Envoltorio delgado sobre `services.attempt_delivery`. Solo se encola con
`WEBHOOKS_USE_QUEUE`; los reintentos con backoff los programa el servicio (una tarea
diferida por intento), así que la tarea no reintenta por su cuenta.
"""

from procrastinate.contrib.django import app

from apps.core.tenancy import tenant_context
from apps.organizations.models import Organization

from . import services
from .models import WebhookDelivery


@app.task(name="webhooks.deliver", queue="webhooks", retry=0)
def deliver_webhook(*, organization_id: str, delivery_id: str) -> str:
    organization = Organization.objects.filter(pk=organization_id, is_active=True).first()
    if organization is None:
        return ""
    with tenant_context(organization):
        delivery = WebhookDelivery.objects.select_related("endpoint").filter(pk=delivery_id).first()
        if delivery is None:
            return ""
        services.attempt_delivery(delivery=delivery)
        return delivery.status
