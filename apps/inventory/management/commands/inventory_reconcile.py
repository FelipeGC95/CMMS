"""Reconciliación de saldos con el libro de movimientos (INV-14).

Uso: python manage.py inventory_reconcile [--organization <slug>] [--fix]

Sin --fix solo informa (la tarea semanal lo usa así y reporta a Sentry por el
log de advertencia); con --fix reescribe saldo y reservado desde el libro y lo audita.
"""

import logging

from django.core.management.base import BaseCommand, CommandError

from apps.core.tenancy import tenant_context
from apps.inventory.services import reconcile_stock
from apps.organizations.models import Organization

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Compara el stock con el libro de movimientos (y lo corrige con --fix)."

    def add_arguments(self, parser):
        parser.add_argument("--organization", help="Identificador (slug) de una organización.")
        parser.add_argument("--fix", action="store_true", help="Reescribe los saldos desde el libro.")

    def handle(self, *args, organization=None, fix=False, **options):
        # Comando de plataforma: recorre organizaciones; cada una se revisa en su propio contexto.
        organizations = Organization.objects.exclude(status=Organization.Status.DELETED).order_by("slug")
        if organization:
            organizations = organizations.filter(slug=organization)
            if not organizations.exists():
                raise CommandError(f"No existe la organización {organization}.")
        total = 0
        for org in organizations:
            with tenant_context(org):
                mismatches = reconcile_stock(fix=fix)
            for mismatch in mismatches:
                self.stdout.write(
                    f"{org.slug}: {mismatch.kind} repuesto={mismatch.part_id} bodega={mismatch.warehouse_id} "
                    f"libro={mismatch.expected} saldo={mismatch.actual} {mismatch.detail}".rstrip()
                )
            if mismatches and not fix:
                logger.warning(
                    "Inventario descuadrado en %s: %s diferencias",
                    org.slug,
                    len(mismatches),
                    extra={"organization_id": str(org.pk)},
                )
            total += len(mismatches)
        action = "corregidas" if fix else "encontradas"
        self.stdout.write(f"Diferencias {action}: {total}")
