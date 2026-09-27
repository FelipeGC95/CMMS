"""Tareas de preventivos (PM-05 CA1): cada hora se generan las OT de cada organización.

Envoltorios delgados sobre `services.generate_due_work_orders`, igual que el
comando `generate_preventive_work_orders`. La generación es idempotente (CA2),
así que una ejecución repetida no duplica OT.
"""

from procrastinate import exceptions as procrastinate_exceptions
from procrastinate.contrib.django import app

from apps.core.tenancy import tenant_context
from apps.maintenance.services import generate_due_work_orders
from apps.organizations.models import Organization


@app.task(name="maintenance.generate_preventive_work_orders", queue="maintenance", retry=3)
def generate_preventive_work_orders(*, organization_id: str) -> int:
    organization = Organization.objects.filter(pk=organization_id, is_active=True).first()
    if organization is None:
        return 0
    with tenant_context(organization):
        return len(generate_due_work_orders())


@app.periodic(cron="0 * * * *", periodic_id="hourly-preventive-generation")
@app.task(name="maintenance.enqueue_preventive_generation", queue="maintenance")
def enqueue_preventive_generation(timestamp: int) -> int:
    """Encola una tarea por organización activa; el lock evita duplicados en cola."""
    enqueued = 0
    for organization_id in Organization.objects.filter(is_active=True).values_list("pk", flat=True):
        try:
            generate_preventive_work_orders.configure(queueing_lock=f"preventive-{organization_id}").defer(
                organization_id=str(organization_id)
            )
        except procrastinate_exceptions.AlreadyEnqueued:
            continue
        enqueued += 1
    return enqueued
