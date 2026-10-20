"""Precios iniciales y organizaciones existentes (ORG-13, ADR-033).

- Precios en UF de Básico, Profesional y Empresa (solo si siguen en blanco: no pisa lo editado).
- Profesional pierde el tope de 300 activos (si sigue en 300): sobre 300 paga por tramos.
- `billing_exempt` llega en True para todas (0019): las creadas por el superadmin y la demo no
  se bloquean. Las del autoregistro en prueba pasan a cobro en línea; si su prueba ya venció, se
  les dan 7 días más desde hoy para que reciban los avisos antes de la gracia.
"""

import datetime
from decimal import Decimal

from django.db import migrations
from django.utils import timezone

PRICES = {
    "basic": {"monthly_price_uf": Decimal("1.5"), "is_self_service": True},
    "pro": {
        "monthly_price_uf": Decimal("4"),
        "price_included_assets": 300,
        "price_block_assets": 100,
        "price_block_uf": Decimal("1"),
        "is_self_service": True,
    },
    # Referencia «desde»: Empresa se cotiza por organización (sin checkout).
    "enterprise": {"monthly_price_uf": Decimal("9"), "is_self_service": False},
}


def forwards(apps, schema_editor):
    Plan = apps.get_model("organizations", "Plan")
    Organization = apps.get_model("organizations", "Organization")
    for code, values in PRICES.items():
        plan = Plan.objects.filter(code=code).first()
        if plan is None or plan.monthly_price_uf is not None:
            continue
        for field, value in values.items():
            setattr(plan, field, value)
        if code == "pro" and plan.max_assets == 300:
            plan.max_assets = None
        plan.save()
    now = timezone.now()
    for organization in Organization.objects.filter(status="trial"):
        organization.billing_exempt = False
        if organization.trial_ends_at is None or organization.trial_ends_at < now:
            organization.trial_ends_at = now + datetime.timedelta(days=7)
        organization.save(update_fields=["billing_exempt", "trial_ends_at"])


class Migration(migrations.Migration):
    dependencies = [("organizations", "0019_billing_fields")]

    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
