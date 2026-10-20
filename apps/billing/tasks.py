"""Tarea diaria de facturación (Procrastinate, ADR-007; ORG-13): envoltorio de `services.run_daily`.

A las 10:00 UTC (06:00–07:00 en Chile): la UF del día ya está publicada y el monto de cada
suscripción queda actualizado antes de los cobros del día. Sin worker, el mismo trabajo lo hace
`manage.py billing_daily` desde un cron.
"""

from procrastinate.contrib.django import app

from . import services


@app.periodic(cron="0 10 * * *", periodic_id="daily-billing")
@app.task(name="billing.run_daily", queue="maintenance", retry=1)
def run_daily(timestamp: int) -> dict:
    return services.run_daily()
