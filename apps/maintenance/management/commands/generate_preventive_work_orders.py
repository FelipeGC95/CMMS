from django.core.management.base import BaseCommand

from apps.core.tenancy import tenant_context
from apps.maintenance.services import generate_due_work_orders
from apps.organizations.models import Organization


class Command(BaseCommand):
    help = "Genera las OT preventivas vencidas o próximas de todas las organizaciones."

    def handle(self, *args, **options):
        total = 0
        for organization in Organization.objects.filter(is_active=True):
            with tenant_context(organization):
                created = generate_due_work_orders()
            total += len(created)
            self.stdout.write(f"{organization}: {len(created)} OT generadas")
        self.stdout.write(self.style.SUCCESS(f"Total: {total}"))
