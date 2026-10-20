"""Facturación de la demo (ORG-13): datos de facturación e historial de pagos de ejemplo.

Las organizaciones demo quedan exentas (facturación manual): nunca se bloquean. Se guarda el
valor de la UF de ejemplo y tres pagos aprobados del plan Profesional para ver el historial.
Idempotente: se puede llamar en cada arranque de Render.
"""

import datetime
from decimal import Decimal

from django.utils import timezone

from apps.billing.models import BillingAccount, Payment, Period, UfValue
from apps.billing.pricing import add_months, to_clp

# Valor de ejemplo: la tarea diaria lo reemplaza por el del día cuando hay red.
DEMO_UF = Decimal("39500.00")


def seed_billing(org) -> None:
    """Corre dentro de `tenant_context(org)`."""
    UfValue.objects.get_or_create(
        date=timezone.localdate() - datetime.timedelta(days=1),
        defaults={"value": DEMO_UF, "source": "demo"},
    )
    if BillingAccount.objects.exists():
        return
    account = BillingAccount.objects.create(
        plan=org.plan,
        period=Period.MONTHLY,
        tax_id="76.123.456-0",
        legal_name=org.name,
        business_activity="Elaboración de alimentos",
        billing_address="Av. Los Carrera 1450, Concepción",
        billing_email="pagos@demo.cl",
    )
    amount_uf = org.plan.monthly_price_uf if org.plan and org.plan.monthly_price_uf else Decimal("4")
    now = timezone.now()
    for months_ago, uf_value in (
        (3, Decimal("39310.12")),
        (2, Decimal("39388.55")),
        (1, Decimal("39467.30")),
    ):
        paid_at = add_months(now, -months_ago)
        Payment.objects.create(
            account=account,
            provider_payment_id=f"demo-{org.slug}-{months_ago}",
            status=Payment.Status.APPROVED,
            status_detail="accredited",
            amount_clp=to_clp(amount_uf, uf_value),
            amount_uf=amount_uf,
            uf_value=uf_value,
            plan=org.plan,
            period=Period.MONTHLY,
            paid_at=paid_at,
            period_end=add_months(paid_at, 1),
        )
