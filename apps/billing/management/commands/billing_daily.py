from django.core.management.base import BaseCommand

from apps.billing import services


class Command(BaseCommand):
    help = (
        "Facturación diaria (ORG-13): guarda la UF del día, actualiza el monto de cada suscripción en "
        "Mercado Pago, aplica bajas de plan programadas y envía los avisos de prueba e impago."
    )

    def handle(self, *args, **options):
        totals = services.run_daily()
        self.stdout.write(
            f"UF: {totals['uf'] or 'sin valor nuevo'} · montos actualizados: {totals['amounts']} · "
            f"avisos: {totals['notices']}"
        )
