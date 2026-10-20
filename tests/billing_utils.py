"""Ayudas para probar la facturación (ORG-13) sin red: proveedor falso y UF guardada."""

import datetime
from decimal import Decimal

from django.utils import timezone

from apps.billing import gateway as gateway_module
from apps.billing import services
from apps.billing.gateway import Checkout, GatewayError, Notification, RemotePayment, RemoteSubscription
from apps.billing.models import UfValue
from apps.billing.uf import chile_today
from apps.organizations.models import Plan
from apps.organizations.plans import ensure_default_plans

UF = Decimal("39500.00")


class FakeGateway:
    """Proveedor en memoria: registra llamadas y responde lo que el test deja en `subscriptions`."""

    name = "mercadopago"

    def __init__(self):
        self.calls = []
        self.subscriptions = {}  # id → RemoteSubscription
        self.payments = {}  # id → RemotePayment
        self.fail = False
        self._next = 0

    def _check(self):
        if self.fail:
            raise GatewayError("caído")

    def create_subscription(self, **kwargs):
        self._check()
        self._next += 1
        subscription_id = f"pre-{self._next}"
        self.calls.append(("create", kwargs))
        self.subscriptions[subscription_id] = RemoteSubscription(
            id=subscription_id,
            status="pending",
            external_reference=kwargs["external_reference"],
            amount_clp=kwargs["amount_clp"],
            next_charge_at=None,
        )
        return Checkout(subscription_id, f"https://www.mercadopago.cl/checkout/{subscription_id}", "pending")

    def get_subscription(self, subscription_id):
        self._check()
        self.calls.append(("get_subscription", subscription_id))
        return self.subscriptions[subscription_id]

    def update_subscription_amount(self, subscription_id, amount_clp):
        self._check()
        self.calls.append(("update_amount", subscription_id, amount_clp))

    def cancel_subscription(self, subscription_id):
        self._check()
        self.calls.append(("cancel", subscription_id))

    def get_payment(self, topic_detail, payment_id):
        self._check()
        self.calls.append(("get_payment", payment_id))
        return self.payments[payment_id]

    def parse_notification(self, *, query, headers, body):
        raise NotImplementedError


def install_gateway(monkeypatch, settings, gateway=None):
    gateway = gateway or FakeGateway()
    settings.MERCADOPAGO_ACCESS_TOKEN = "TEST-token"
    settings.MERCADOPAGO_WEBHOOK_SECRET = "secreto"
    monkeypatch.setattr(services, "get_gateway", lambda: gateway)
    monkeypatch.setattr(gateway_module, "get_gateway", lambda: gateway)
    return gateway


def store_uf(value=UF, date=None):
    return UfValue.objects.update_or_create(
        date=date or chile_today(), defaults={"value": value, "source": "test"}
    )[0]


def plans():
    """Básico y Profesional con sus precios (los fija la migración 0020; ensure por si acaso)."""
    ensure_default_plans(Plan)
    return Plan.objects.get(code="basic"), Plan.objects.get(code="pro")


def make_billable(org, *, trial_days=10):
    """Organización del autoregistro: sin exención y en prueba."""
    from apps.organizations.models import Organization

    org.billing_exempt = False
    org.status = Organization.Status.TRIAL
    org.trial_ends_at = timezone.now() + datetime.timedelta(days=trial_days)
    org.plan = Plan.objects.get(code="trial")
    org.save()
    return org


def authorize(gateway, subscription_id, *, next_days=30):
    old = gateway.subscriptions[subscription_id]
    gateway.subscriptions[subscription_id] = RemoteSubscription(
        id=old.id,
        status="authorized",
        external_reference=old.external_reference,
        amount_clp=old.amount_clp,
        next_charge_at=timezone.now() + datetime.timedelta(days=next_days),
    )
    return services.handle_notification(
        notification=Notification(f"sub:{subscription_id}:auth", "subscription", subscription_id),
        gateway=gateway,
    )


def pay(gateway, subscription_id, payment_id, *, status="approved", amount=59250, key=None):
    gateway.payments[payment_id] = RemotePayment(
        id=payment_id,
        subscription_id=subscription_id,
        status=status,
        status_detail="accredited" if status == "approved" else "cc_rejected_insufficient_amount",
        amount_clp=amount,
        paid_at=timezone.now(),
    )
    return services.handle_notification(
        notification=Notification(
            key or f"pay:{payment_id}:{status}", "payment", payment_id, "subscription_authorized_payment"
        ),
        gateway=gateway,
    )
