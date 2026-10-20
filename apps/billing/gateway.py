"""Interfaz pequeña con el proveedor de pagos (ORG-13, ADR-033).

Hoy solo Mercado Pago (`mercadopago.MercadoPagoGateway`). Si mañana se suma Flow o Stripe, basta
otra clase con estos métodos; los servicios solo hablan con `PaymentGateway`. Sin credenciales,
`get_gateway()` devuelve None y el cobro en línea queda inactivo (ADR-020).
"""

import datetime
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from django.conf import settings


class GatewayError(Exception):
    """El proveedor no respondió o respondió un error. El mensaje no lleva secretos."""


class InvalidSignature(Exception):
    """El webhook no trae una firma válida del proveedor."""


@dataclass(frozen=True)
class Checkout:
    subscription_id: str
    checkout_url: str
    status: str


@dataclass(frozen=True)
class RemoteSubscription:
    id: str
    status: str  # pending | authorized | paused | cancelled
    external_reference: str
    amount_clp: int | None
    next_charge_at: datetime.datetime | None


@dataclass(frozen=True)
class RemotePayment:
    id: str
    subscription_id: str  # vacío si el pago no es de una suscripción
    status: str  # approved | pending | rejected | refunded | cancelled
    status_detail: str
    amount_clp: int
    paid_at: datetime.datetime | None


@dataclass(frozen=True)
class Notification:
    event_key: str
    topic: str  # subscription | payment | vacío (tipo que no nos interesa: se ignora)
    resource_id: str
    detail: str = ""  # tipo propio del proveedor (p. ej. "subscription_authorized_payment")


TOPIC_SUBSCRIPTION = "subscription"
TOPIC_PAYMENT = "payment"


class PaymentGateway(Protocol):
    name: str

    def create_subscription(
        self,
        *,
        reason: str,
        external_reference: str,
        payer_email: str,
        amount_clp: int,
        frequency_months: int,
        back_url: str,
    ) -> Checkout: ...

    def get_subscription(self, subscription_id: str) -> RemoteSubscription: ...

    def update_subscription_amount(self, subscription_id: str, amount_clp: int) -> None: ...

    def cancel_subscription(self, subscription_id: str) -> None: ...

    def get_payment(self, topic_detail: str, payment_id: str) -> RemotePayment: ...

    def parse_notification(self, *, query: dict, headers: dict, body: bytes) -> Notification: ...


def is_configured() -> bool:
    return bool(settings.MERCADOPAGO_ACCESS_TOKEN and settings.MERCADOPAGO_WEBHOOK_SECRET)


def get_gateway() -> PaymentGateway | None:
    """El proveedor configurado, o None si faltan las credenciales (cobro en línea inactivo)."""
    if not is_configured():
        return None
    from .mercadopago import MercadoPagoGateway

    return MercadoPagoGateway(
        access_token=settings.MERCADOPAGO_ACCESS_TOKEN,
        webhook_secret=settings.MERCADOPAGO_WEBHOOK_SECRET,
        api_url=settings.MERCADOPAGO_API_URL,
    )


def as_int_clp(value) -> int | None:
    if value is None:
        return None
    try:
        return int(Decimal(str(value)).to_integral_value())
    except (ArithmeticError, ValueError):
        return None
