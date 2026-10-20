"""Purga de entregas de webhook antiguas (X-08, docs/07).

Uso: python manage.py purge_webhook_deliveries [--organization <slug>] [--days 90]

Borra las entregas terminadas (entregadas o fallidas) de más de `--days` días; las
pendientes no se tocan. La tarea diaria `webhooks.enqueue_purge` hace lo mismo con cola.
"""

from django.core.management.base import BaseCommand, CommandError

from apps.core.tenancy import tenant_context
from apps.organizations.models import Organization
from apps.webhooks.services import DELIVERY_RETENTION_DAYS, purge_old_deliveries


class Command(BaseCommand):
    help = "Borra las entregas de webhook terminadas de más de 90 días."

    def add_arguments(self, parser):
        parser.add_argument("--organization", help="Identificador (slug) de una organización.")
        parser.add_argument("--days", type=int, default=DELIVERY_RETENTION_DAYS, help="Antigüedad en días.")

    def handle(self, *args, organization=None, days=DELIVERY_RETENTION_DAYS, **options):
        if days < 1:
            raise CommandError("--days debe ser al menos 1.")
        # Comando de plataforma: recorre organizaciones; cada una se purga en su propio contexto.
        organizations = Organization.objects.order_by("slug")
        if organization:
            organizations = organizations.filter(slug=organization)
            if not organizations.exists():
                raise CommandError(f"No existe la organización {organization}.")
        total = 0
        for org in organizations:
            with tenant_context(org):
                deleted = purge_old_deliveries(days=days)
            if deleted:
                self.stdout.write(f"{org.slug}: {deleted} entregas borradas")
            total += deleted
        self.stdout.write(f"Entregas borradas: {total}")
