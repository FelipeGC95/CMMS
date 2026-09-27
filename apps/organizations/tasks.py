"""Tareas de la organización (Procrastinate, ADR-007): exportación completa y purga (X-03, X-04).

Envoltorios delgados sobre `services` y `purge`.
"""

from procrastinate.contrib.django import app

from apps.core.tenancy import tenant_context

from . import purge, services
from .models import Organization


@app.task(name="organizations.build_data_export", queue="exports", retry=1)
def build_data_export(*, organization_id: str, export_id: str) -> str:
    organization = Organization.objects.filter(pk=organization_id, is_active=True).first()
    if organization is None:
        return ""
    with tenant_context(organization):
        export = services.build_data_export(export_id=export_id)
        return export.status if export else ""


# Diario a las 08:00 UTC: vence exportaciones y purga organizaciones con el borrado vencido.
@app.periodic(cron="0 8 * * *", periodic_id="daily-organization-purge")
@app.task(name="organizations.purge_organizations", queue="maintenance", retry=1)
def purge_organizations(timestamp: int) -> int:
    return purge.run_daily()
