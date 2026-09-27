from django.core.management.base import BaseCommand

from apps.attachments.services import extract_pending_documents
from apps.core.tenancy import tenant_context
from apps.organizations.models import Organization


class Command(BaseCommand):
    help = "Extrae el texto de los manuales PDF pendientes de todas las organizaciones (AST-06 CA3)."

    def add_arguments(self, parser):
        parser.add_argument("--retry-failed", action="store_true", help="Reintenta también los fallidos.")

    def handle(self, *args, **options):
        total = 0
        for organization in Organization.objects.filter(is_active=True):
            with tenant_context(organization):
                processed = extract_pending_documents(retry_failed=options["retry_failed"])
            total += processed
            if processed:
                self.stdout.write(f"{organization}: {processed} manuales procesados")
        self.stdout.write(self.style.SUCCESS(f"Total: {total}"))
