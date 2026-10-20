"""Webhooks en el worker (Procrastinate, ADR-007/ADR-030).

Envoltorios delgados sobre los servicios: `deliver_webhook` (un intento de entrega; los
reintentos con backoff los programa el servicio, así que la tarea no reintenta por su
cuenta), `dispatch_events_task` (lote de eventos de una importación masiva) y la purga
diaria de entregas antiguas. Las dos primeras solo se encolan con `WEBHOOKS_USE_QUEUE`.
"""

import logging

from procrastinate import exceptions as procrastinate_exceptions
from procrastinate.contrib.django import app

from apps.core.tenancy import tenant_context
from apps.organizations.models import Organization

from . import services
from .models import WebhookDelivery

logger = logging.getLogger(__name__)


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


@app.task(name="webhooks.dispatch_events", queue="webhooks", retry=2)
def dispatch_events_task(*, organization_id: str, events: list) -> int:
    """Crea y encola las entregas de un lote de eventos (importación masiva)."""
    organization = Organization.objects.filter(pk=organization_id, is_active=True).first()
    if organization is None:
        return 0
    with tenant_context(organization):
        return len(services.dispatch_events([tuple(event) for event in events]))


@app.task(name="webhooks.purge_deliveries", queue="maintenance", retry=2)
def purge_organization_deliveries(*, organization_id: str) -> int:
    organization = Organization.objects.filter(pk=organization_id).first()
    if organization is None:
        return 0
    with tenant_context(organization):
        return services.purge_old_deliveries()


# Diario a las 08:30 UTC (madrugada en Chile): una tarea por organización; el lock evita duplicados.
@app.periodic(cron="30 8 * * *", periodic_id="daily-webhook-delivery-purge")
@app.task(name="webhooks.enqueue_purge", queue="maintenance")
def enqueue_delivery_purge(timestamp: int) -> int:
    enqueued = 0
    for organization_id in Organization.objects.values_list("pk", flat=True):
        try:
            purge_organization_deliveries.configure(queueing_lock=f"webhook-purge-{organization_id}").defer(
                organization_id=str(organization_id)
            )
        except procrastinate_exceptions.AlreadyEnqueued:
            continue
        enqueued += 1
    logger.info("Purga de entregas de webhook encolada para %s organizaciones.", enqueued)
    return enqueued
