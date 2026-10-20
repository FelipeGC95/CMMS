"""Cliente propio y delgado de Mercado Pago: suscripciones (`/preapproval`) en CLP y webhooks firmados.

Sin SDK: cuatro llamadas REST con `http.request_json` (timeout, SSRF, sin redirecciones). El token
va solo en la cabecera `Authorization` y nunca se registra. Referencias de la API:
- POST/GET/PUT `/preapproval` (suscripción sin plan asociado, pago pendiente → `init_point`).
- GET `/authorized_payments/{id}` (cobro de una suscripción) y GET `/v1/payments/{id}`.
- Firma `x-signature: ts=…,v1=…` = HMAC-SHA256(secreto, "id:<data.id>;request-id:<x-request-id>;ts:<ts>;").
"""

import datetime
import hashlib
import hmac
import json
import uuid

from . import http
from .gateway import (
    TOPIC_PAYMENT,
    TOPIC_SUBSCRIPTION,
    Checkout,
    GatewayError,
    InvalidSignature,
    Notification,
    RemotePayment,
    RemoteSubscription,
    as_int_clp,
)

# Tipos de notificación de Mercado Pago → tema genérico.
TOPICS = {
    "subscription_preapproval": TOPIC_SUBSCRIPTION,
    "preapproval": TOPIC_SUBSCRIPTION,
    "subscription_authorized_payment": TOPIC_PAYMENT,
    "authorized_payment": TOPIC_PAYMENT,
    "payment": TOPIC_PAYMENT,
}
AUTHORIZED_PAYMENT_TYPES = {"subscription_authorized_payment", "authorized_payment"}
PAYMENT_STATUSES = {
    "approved": "approved",
    "authorized": "pending",
    "pending": "pending",
    "in_process": "pending",
    "in_mediation": "pending",
    "rejected": "rejected",
    "cancelled": "cancelled",
    "refunded": "refunded",
    "charged_back": "refunded",
}
SUBSCRIPTION_STATUSES = {"pending", "authorized", "paused", "cancelled"}


def _parse_datetime(value) -> datetime.datetime | None:
    if not value:
        return None
    try:
        moment = datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=datetime.UTC)


def _dict(value) -> dict:
    return value if isinstance(value, dict) else {}


def signature_manifest(*, data_id: str, request_id: str, ts: str) -> str:
    """Plantilla de la firma de Mercado Pago; las partes ausentes se omiten."""
    if data_id.isalnum():
        data_id = data_id.lower()
    parts = []
    if data_id:
        parts.append(f"id:{data_id};")
    if request_id:
        parts.append(f"request-id:{request_id};")
    if ts:
        parts.append(f"ts:{ts};")
    return "".join(parts)


def sign_manifest(secret: str, manifest: str) -> str:
    return hmac.new(secret.encode(), manifest.encode(), hashlib.sha256).hexdigest()


class MercadoPagoGateway:
    name = "mercadopago"

    def __init__(self, *, access_token: str, webhook_secret: str, api_url: str):
        self._token = access_token
        self._secret = webhook_secret
        self._api = api_url.rstrip("/")

    # --- HTTP -------------------------------------------------------------------------------

    def _call(self, method: str, path: str, payload=None, *, idempotent=False) -> dict:
        headers = {"Authorization": f"Bearer {self._token}"}
        if idempotent:
            headers["X-Idempotency-Key"] = str(uuid.uuid4())
        try:
            status, data = http.request_json(method, f"{self._api}{path}", headers=headers, payload=payload)
        except http.HttpError as error:
            raise GatewayError(f"Mercado Pago no respondió ({error})") from error
        if not 200 <= status < 300 or not isinstance(data, dict):
            message = data.get("message", "") if isinstance(data, dict) else ""
            raise GatewayError(f"Mercado Pago respondió {status}: {str(message)[:200]}")
        return data

    # --- Suscripciones ------------------------------------------------------------------------

    def create_subscription(
        self, *, reason, external_reference, payer_email, amount_clp, frequency_months, back_url
    ) -> Checkout:
        data = self._call(
            "POST",
            "/preapproval",
            {
                "reason": reason[:250],
                "external_reference": external_reference,
                "payer_email": payer_email,
                "back_url": back_url,
                "status": "pending",
                "auto_recurring": {
                    "frequency": frequency_months,
                    "frequency_type": "months",
                    "transaction_amount": amount_clp,
                    "currency_id": "CLP",
                },
            },
            idempotent=True,
        )
        if not data.get("id") or not data.get("init_point"):
            raise GatewayError("Mercado Pago no devolvió el enlace de pago")
        return Checkout(
            subscription_id=str(data["id"]),
            checkout_url=str(data["init_point"]),
            status=str(data.get("status", "pending")),
        )

    def get_subscription(self, subscription_id: str) -> RemoteSubscription:
        data = self._call("GET", f"/preapproval/{subscription_id}")
        recurring = data.get("auto_recurring") if isinstance(data.get("auto_recurring"), dict) else {}
        status = str(data.get("status", ""))
        return RemoteSubscription(
            id=str(data.get("id", "")),
            status=status if status in SUBSCRIPTION_STATUSES else "",
            external_reference=str(data.get("external_reference") or ""),
            amount_clp=as_int_clp(recurring.get("transaction_amount")),
            next_charge_at=_parse_datetime(data.get("next_payment_date")),
        )

    def update_subscription_amount(self, subscription_id: str, amount_clp: int) -> None:
        self._call(
            "PUT",
            f"/preapproval/{subscription_id}",
            {"auto_recurring": {"transaction_amount": amount_clp, "currency_id": "CLP"}},
        )

    def cancel_subscription(self, subscription_id: str) -> None:
        self._call("PUT", f"/preapproval/{subscription_id}", {"status": "cancelled"})

    # --- Pagos --------------------------------------------------------------------------------

    def get_payment(self, topic_detail: str, payment_id: str) -> RemotePayment:
        if topic_detail in AUTHORIZED_PAYMENT_TYPES:
            data = self._call("GET", f"/authorized_payments/{payment_id}")
            payment = data.get("payment") if isinstance(data.get("payment"), dict) else {}
            status = PAYMENT_STATUSES.get(str(payment.get("status", "")), "")
            if not status and data.get("status") == "recycling":
                status = "rejected"  # cobro fallido que Mercado Pago reintentará
            return RemotePayment(
                id=str(payment.get("id") or ""),
                subscription_id=str(data.get("preapproval_id") or ""),
                status=status,
                status_detail=str(payment.get("status_detail") or data.get("status") or "")[:100],
                amount_clp=as_int_clp(data.get("transaction_amount")) or 0,
                paid_at=_parse_datetime(data.get("debit_date") or data.get("date_created")),
            )
        data = self._call("GET", f"/v1/payments/{payment_id}")
        metadata = _dict(data.get("metadata"))
        transaction = _dict(_dict(data.get("point_of_interaction")).get("transaction_data"))
        subscription_id = metadata.get("preapproval_id") or transaction.get("subscription_id") or ""
        return RemotePayment(
            id=str(data.get("id") or ""),
            subscription_id=str(subscription_id),
            status=PAYMENT_STATUSES.get(str(data.get("status", "")), ""),
            status_detail=str(data.get("status_detail") or "")[:100],
            amount_clp=as_int_clp(data.get("transaction_amount")) or 0,
            paid_at=_parse_datetime(data.get("date_approved") or data.get("date_created")),
        )

    # --- Webhooks -----------------------------------------------------------------------------

    def parse_notification(self, *, query: dict, headers: dict, body: bytes) -> Notification:
        """Valida `x-signature` y devuelve qué recurso consultar. No confía en nada más del cuerpo."""
        try:
            payload = json.loads(body or b"{}")
        except ValueError:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        data_id = str(query.get("data.id") or data.get("id") or query.get("id") or "").strip()
        topic_detail = str(query.get("type") or payload.get("type") or query.get("topic") or "").strip()
        signature = headers.get("x-signature", "")
        request_id = headers.get("x-request-id", "")
        fields = dict(part.strip().split("=", 1) for part in signature.split(",") if "=" in part)
        ts, received = fields.get("ts", ""), fields.get("v1", "")
        if not ts or not received or not data_id:
            raise InvalidSignature("Falta la firma o el id")
        manifest = signature_manifest(data_id=data_id, request_id=request_id, ts=ts)
        expected = sign_manifest(self._secret, manifest)
        if not hmac.compare_digest(expected, received.lower()):
            raise InvalidSignature("Firma inválida")
        event_id = str(payload.get("id") or request_id or ts)
        return Notification(
            event_key=f"{topic_detail}:{event_id}:{data_id}"[:150],
            topic=TOPICS.get(topic_detail, ""),
            resource_id=data_id[:64],
            detail=topic_detail[:60],
        )
