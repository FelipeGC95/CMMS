"""Aviso diario de OT por vencer y vencidas (X-02), por organización.

Envoltorios delgados sobre `services.send_due_notifications`, igual que el
comando `send_due_notifications` (para cron cuando no hay worker). El envío
es idempotente por OT, persona y día.
"""

from procrastinate import exceptions as procrastinate_exceptions
from procrastinate.contrib.django import app

from apps.core.tenancy import tenant_context
from apps.organizations.models import Organization

from .services import send_due_notifications


@app.task(name="notifications.send_due_notifications", queue="notifications", retry=3)
def send_due_notifications_for(*, organization_id: str) -> int:
    organization = Organization.objects.filter(pk=organization_id, is_active=True).first()
    if organization is None:
        return 0
    with tenant_context(organization):
        return send_due_notifications()


# Diario a las 11:00 UTC (07:00-08:00 en Chile): antes del inicio del turno.
@app.periodic(cron="0 11 * * *", periodic_id="daily-due-notifications")
@app.task(name="notifications.enqueue_due_notifications", queue="notifications")
def enqueue_due_notifications(timestamp: int) -> int:
    """Encola una tarea por organización activa; el lock evita duplicados en cola."""
    enqueued = 0
    for organization_id in Organization.objects.filter(is_active=True).values_list("pk", flat=True):
        try:
            send_due_notifications_for.configure(queueing_lock=f"due-notifications-{organization_id}").defer(
                organization_id=str(organization_id)
            )
        except procrastinate_exceptions.AlreadyEnqueued:
            continue
        enqueued += 1
    return enqueued
