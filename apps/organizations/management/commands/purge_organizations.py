"""Vence exportaciones y purga organizaciones con el borrado vencido (X-03, X-04).

Uso: python manage.py purge_organizations [--dry-run]
Lo mismo corre a diario en el worker (`organizations.purge_organizations`); sin
worker, un cron diario llama a este comando. Ver docs/07_operacion.md.
"""

from django.core.management.base import BaseCommand

from apps.organizations import purge


class Command(BaseCommand):
    help = "Borra datos y archivos de las organizaciones cuyo período de gracia terminó."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Solo lista lo que se purgaría.")

    def handle(self, *args, dry_run=False, **options):
        due = list(purge.due_organizations())
        if dry_run:
            for organization in due:
                self.stdout.write(
                    f"{organization.pk} · {organization.name} · {organization.deletion_scheduled_for}"
                )
            self.stdout.write(f"{len(due)} organizaciones por purgar.")
            return
        expired = purge.expire_all_data_exports()
        purged = purge.purge_due_organizations()
        self.stdout.write(
            self.style.SUCCESS(f"{expired} exportaciones vencidas; {len(purged)} organizaciones purgadas.")
        )
