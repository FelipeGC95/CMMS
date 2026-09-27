from django.core.management.base import BaseCommand

from apps.core.tenancy import tenant_context
from apps.notifications.services import send_due_notifications
from apps.organizations.models import Organization


class Command(BaseCommand):
    help = (
        "Avisa las OT por vencer y vencidas de todas las organizaciones (X-02). "
        "Para cron sin worker; idempotente por OT, persona y día."
    )

    def handle(self, *args, **options):
        total = 0
        # Comando de plataforma: recorre todas las organizaciones activas, cada una en su contexto.
        for organization in Organization.objects.filter(is_active=True):
            with tenant_context(organization):
                created = send_due_notifications()
            total += created
            self.stdout.write(f"{organization}: {created} notificaciones")
        self.stdout.write(self.style.SUCCESS(f"Total: {total}"))
