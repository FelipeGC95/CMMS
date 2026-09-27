"""Tareas de plataforma (Procrastinate, ADR-007): correo y respaldo.

Envoltorios delgados: la lógica vive en `apps.core.mail` y `apps.core.backups`.
No tocan datos de negocio, así que no fijan organización.
"""

import logging

from django.conf import settings
from procrastinate import builtin_tasks
from procrastinate.contrib.django import app

from apps.core import backups, mail

logger = logging.getLogger(__name__)


@app.task(name="core.send_email", queue="email", retry=5)
def send_email(*, message: dict) -> int:
    return mail.deliver_serialized(message)


# Diario a las 07:00 UTC (madrugada en Chile). Sin BACKUPS_ENABLED=1 no hace nada.
@app.periodic(cron="0 7 * * *", periodic_id="daily-backup")
@app.task(name="core.backup_database", queue="maintenance", retry=2)
def backup_database(timestamp: int) -> str | None:
    if not settings.BACKUPS_ENABLED:
        logger.info("Respaldo diario omitido: BACKUPS_ENABLED no está activo.")
        return None
    return backups.run_backup()


# Limpieza de la cola: los trabajos terminados con éxito se borran a los 7 días.
@app.periodic(cron="30 6 * * *", periodic_id="daily-queue-cleanup")
@app.task(
    name="core.remove_old_jobs", queue="maintenance", queueing_lock="remove_old_jobs", pass_context=True
)
async def remove_old_jobs(context, timestamp: int) -> None:
    await builtin_tasks.remove_old_jobs(context, max_hours=7 * 24)
