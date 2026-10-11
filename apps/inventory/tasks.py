"""Tareas de inventario (INV-14 CA4): reconciliación semanal del saldo con el libro.

Envoltorios delgados sobre `services.reconcile_stock` (sin `--fix`: solo informa), igual
que el comando `inventory_reconcile`. Una diferencia se reporta con `logger.warning`
(llega a los logs del worker y a Sentry); se corrige a mano (docs/07, sección 3e).
"""

import logging

from procrastinate import exceptions as procrastinate_exceptions
from procrastinate.contrib.django import app

from apps.core.tenancy import tenant_context
from apps.organizations.models import Organization

from .services import reconcile_stock

logger = logging.getLogger(__name__)


@app.task(name="inventory.reconcile_stock", queue="maintenance", retry=2)
def reconcile_organization_stock(*, organization_id: str) -> int:
    """Diferencias encontradas en una organización (0 si cuadra o si ya no está activa)."""
    organization = Organization.objects.filter(pk=organization_id, is_active=True).first()
    if organization is None:
        return 0
    with tenant_context(organization):
        mismatches = reconcile_stock(fix=False)
    if mismatches:
        logger.warning(
            "Inventario descuadrado en %s: %s diferencias (%s). Revisar con inventory_reconcile.",
            organization.slug,
            len(mismatches),
            ", ".join(sorted({mismatch.kind for mismatch in mismatches})),
            extra={"organization_id": str(organization.pk)},
        )
    return len(mismatches)


# Semanal: lunes 09:00 UTC (madrugada en Chile), después de los respaldos.
@app.periodic(cron="0 9 * * 1", periodic_id="weekly-inventory-reconcile")
@app.task(name="inventory.enqueue_reconciliation", queue="maintenance")
def enqueue_reconciliation(timestamp: int) -> int:
    """Encola una reconciliación por organización activa; el lock evita duplicados en cola."""
    enqueued = 0
    for organization_id in Organization.objects.filter(is_active=True).values_list("pk", flat=True):
        try:
            reconcile_organization_stock.configure(
                queueing_lock=f"inventory-reconcile-{organization_id}"
            ).defer(organization_id=str(organization_id))
        except procrastinate_exceptions.AlreadyEnqueued:
            continue
        enqueued += 1
    return enqueued
