"""Webhooks salientes (X-08, ADR-030): alta de destinos, disparo de eventos y entrega firmada.

Los servicios de negocio llaman a `emit_event` dentro de su transacción; el evento se
arma en `transaction.on_commit` (si la escritura se revierte, no sale nada) y crea una
entrega por destino activo suscrito. Con `WEBHOOKS_USE_QUEUE` la envía el worker con
reintentos y backoff; si no, se intenta una vez al confirmar (demo sin worker).

Contra SSRF: solo `https`, y el dominio debe resolver a direcciones públicas, al guardar
y en cada envío; la conexión va a la dirección ya validada (sin re-resolver) y no se
siguen redirecciones.
"""

import datetime
import hashlib
import hmac
import http.client
import ipaddress
import json
import secrets
import socket
import ssl
import time
import uuid
from urllib.parse import urlsplit

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext as _

from apps.accounts.permissions import Perm, require_perm
from apps.audit import services as audit
from apps.audit.models import AuditEvent
from apps.core.tenancy import require_current_organization, tenant_context
from apps.organizations.modules import has_module

from .models import TEST_EVENT, Event, WebhookDelivery, WebhookEndpoint

MODULE = "api"  # los webhooks van con la API (Profesional y Empresa, ADR-030)
SECRET_PREFIX = "whsec_"
SIGNATURE_HEADER = "X-Galbust-Signature"
TIMEOUT_SECONDS = 5
MAX_ATTEMPTS = 5
# Espera antes del intento 2, 3, 4 y 5 (segundos): 1 min, 5 min, 30 min, 2 h.
BACKOFF_SECONDS = (60, 300, 1800, 7200)
DISABLE_AFTER_FAILURES = 5  # entregas agotadas seguidas que desactivan el destino
BLOCKED_HOST_SUFFIXES = (".localhost", ".local", ".internal", ".lan", ".home.arpa")


class WebhookError(Exception):
    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


class UnsafeURLError(WebhookError):
    """La URL no es https o apunta a una dirección privada, de loopback o link-local."""


# --- Validación de destino (SSRF) ----------------------------------------------------------


def resolve_host(host: str, port: int) -> list[str]:
    """Direcciones IP del dominio (los tests reemplazan esta función: sin DNS real)."""
    return [info[4][0] for info in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)]


def _is_public(address: str) -> bool:
    ip = ipaddress.ip_address(address.split("%", 1)[0])
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


def public_address(url: str) -> str:
    """Primera dirección del destino si todas son públicas; si no, `UnsafeURLError`."""
    parts = urlsplit(url)
    if parts.scheme != "https":
        raise UnsafeURLError(_("La URL debe empezar con https://."))
    host = (parts.hostname or "").rstrip(".").lower()
    if not host or host == "localhost" or host.endswith(BLOCKED_HOST_SUFFIXES):
        raise UnsafeURLError(_("La URL debe apuntar a un dominio público."))
    try:
        port = parts.port or 443
        addresses = resolve_host(host, port)
    except (OSError, ValueError, UnicodeError) as error:
        raise UnsafeURLError(_("No se pudo resolver el dominio de la URL.")) from error
    if not addresses or not all(_is_public(address) for address in addresses):
        raise UnsafeURLError(_("La URL apunta a una dirección privada o interna; no está permitida."))
    return addresses[0]


# --- Envío HTTP ------------------------------------------------------------------------------


class PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS a una IP ya validada, con el certificado y el SNI del dominio original."""

    def __init__(self, host, *, address, **kwargs):
        super().__init__(host, **kwargs)
        self._address = address

    def connect(self):
        sock = socket.create_connection((self._address, self.port), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


def http_post(url: str, *, address: str, body: bytes, headers: dict) -> int:
    """POST con timeout corto; devuelve el código HTTP (no sigue redirecciones)."""
    parts = urlsplit(url)
    path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    connection = PinnedHTTPSConnection(
        parts.hostname,
        address=address,
        port=parts.port or 443,
        timeout=TIMEOUT_SECONDS,
        context=ssl.create_default_context(),
    )
    try:
        connection.request("POST", path, body=body, headers=headers)
        response = connection.getresponse()
        response.read(4096)
        return response.status
    finally:
        connection.close()


def sign(secret: str, timestamp: int, body: bytes) -> str:
    """HMAC-SHA256 en hex de `"<timestamp>.<cuerpo>"` con el secreto del destino."""
    return hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()


def encode_payload(payload: dict) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()


# --- Destinos ---------------------------------------------------------------------------------


def _clean_events(events) -> list[str]:
    events = [code for code in Event.values if code in set(events)]
    if not events:
        raise WebhookError(_("Elige al menos un evento."))
    return events


@transaction.atomic
def create_endpoint(*, membership, name: str, url: str, events) -> WebhookEndpoint:
    require_perm(membership, Perm.API_TOKEN_MANAGE)
    public_address(url)
    endpoint = WebhookEndpoint.objects.create(
        name=name.strip(),
        url=url.strip(),
        events=_clean_events(events),
        secret=SECRET_PREFIX + secrets.token_urlsafe(32),
        created_by=membership.user,
    )
    audit.record(
        action=AuditEvent.Action.CREATE,
        target=endpoint,
        membership=membership,
        changes=audit.snapshot(endpoint, fields={"name", "url", "events", "is_active"}),
    )
    return endpoint


@transaction.atomic
def update_endpoint(
    *, membership, endpoint: WebhookEndpoint, name: str, url: str, events, is_active: bool
) -> WebhookEndpoint:
    """Edita el destino. Reactivarlo reinicia el contador de fallas."""
    require_perm(membership, Perm.API_TOKEN_MANAGE)
    url = url.strip()
    if url != endpoint.url:
        public_address(url)
    before = audit.snapshot(endpoint, fields={"name", "url", "events", "is_active"})
    endpoint.name, endpoint.url, endpoint.events = name.strip(), url, _clean_events(events)
    if is_active and not endpoint.is_active:
        endpoint.consecutive_failures = 0
        endpoint.disabled_at = None
    endpoint.is_active = is_active
    endpoint.save()
    after = audit.snapshot(endpoint, fields={"name", "url", "events", "is_active"})
    changes = {key: [before[key], value] for key, value in after.items() if before[key] != value}
    if changes:
        action = AuditEvent.Action.UPDATE
        if set(changes) == {"is_active"}:
            action = AuditEvent.Action.REACTIVATE if is_active else AuditEvent.Action.DEACTIVATE
        audit.record(action=action, target=endpoint, membership=membership, changes=changes)
    return endpoint


# --- Eventos ----------------------------------------------------------------------------------


def emit_event(*, event_type: str, instance) -> None:
    """Programa el evento para cuando la transacción se confirme (sin consultas antes)."""
    organization = require_current_organization()
    model, pk = type(instance), instance.pk

    def run():
        with tenant_context(organization):
            dispatch(event_type=event_type, model=model, pk=pk)

    transaction.on_commit(run, robust=True)


def _object_data(model, pk) -> dict | None:
    from rest_framework.utils.encoders import JSONEncoder

    from apps.api.serializers import WorkOrderSerializer, WorkRequestSerializer
    from apps.work.models import WorkOrder, WorkRequest

    if model is WorkOrder:
        instance = WorkOrder.objects.filter(pk=pk).first()
        data = WorkOrderSerializer(instance).data if instance else None
    elif model is WorkRequest:
        instance = WorkRequest.objects.select_related("work_order").filter(pk=pk).first()
        data = WorkRequestSerializer(instance).data if instance else None
    else:  # pragma: no cover - solo hay eventos de OT y solicitudes
        raise ValueError(model)
    # Mismos campos y formato que la API v1 (UUID, fechas y decimales como texto).
    return json.loads(json.dumps(data, cls=JSONEncoder)) if data is not None else None


def build_payload(*, event_type: str, data: dict) -> dict:
    organization = require_current_organization()
    return {
        "id": str(uuid.uuid4()),
        "type": event_type,
        "created_at": timezone.now().astimezone(datetime.UTC).isoformat().replace("+00:00", "Z"),
        "organization": {"id": str(organization.pk), "slug": organization.slug, "name": organization.name},
        "data": data,
    }


def dispatch(*, event_type: str, model, pk) -> list[WebhookDelivery]:
    """Crea y programa una entrega por destino activo suscrito al evento."""
    organization = require_current_organization()
    if not has_module(organization, MODULE):
        return []
    # El filtro por evento va en Python: pocos destinos por organización y sirve en SQLite.
    endpoints = [e for e in WebhookEndpoint.objects.filter(is_active=True) if event_type in e.events]
    if not endpoints:
        return []
    data = _object_data(model, pk)
    if data is None:
        return []
    payload = build_payload(event_type=event_type, data=data)
    deliveries = WebhookDelivery.objects.bulk_create(
        [
            WebhookDelivery(
                organization=organization,
                endpoint=endpoint,
                event_id=payload["id"],
                event_type=event_type,
                payload=payload,
            )
            for endpoint in endpoints
        ]
    )
    for delivery, endpoint in zip(deliveries, endpoints, strict=True):
        delivery.endpoint = endpoint
        schedule_delivery(delivery=delivery)
    return deliveries


@transaction.atomic
def send_test(*, membership, endpoint: WebhookEndpoint) -> WebhookDelivery:
    """«Enviar prueba»: un evento `webhook.test` solo a este destino (sin reintentos)."""
    require_perm(membership, Perm.API_TOKEN_MANAGE)
    if not endpoint.is_active:
        raise WebhookError(_("Activa el webhook antes de enviar una prueba."))
    payload = build_payload(event_type=TEST_EVENT, data={"message": "GALBUST CMMS webhook test"})
    delivery = WebhookDelivery.objects.create(
        endpoint=endpoint, event_id=payload["id"], event_type=TEST_EVENT, payload=payload
    )
    organization = require_current_organization()

    def run():
        with tenant_context(organization):
            schedule_delivery(delivery=delivery)

    transaction.on_commit(run, robust=True)
    return delivery


# --- Entrega ----------------------------------------------------------------------------------


def _enqueue(*, delivery: WebhookDelivery, delay: int | None) -> None:
    from .tasks import deliver_webhook

    task = deliver_webhook.configure(schedule_in={"seconds": delay}) if delay else deliver_webhook
    task.defer(organization_id=str(delivery.organization_id), delivery_id=str(delivery.pk))


def schedule_delivery(*, delivery: WebhookDelivery, delay: int | None = None) -> None:
    """Al worker (con espera para reintentos) o, sin cola, un intento inmediato."""
    if settings.WEBHOOKS_USE_QUEUE:
        _enqueue(delivery=delivery, delay=delay)
    elif delay is None:
        attempt_delivery(delivery=delivery)


def attempt_delivery(*, delivery: WebhookDelivery, now=None) -> int | None:
    """Un intento de entrega. Devuelve los segundos hasta el reintento, o None si terminó."""
    endpoint = delivery.endpoint
    if delivery.status != WebhookDelivery.Status.PENDING:
        return None
    now = now or timezone.now()
    is_test = delivery.event_type == TEST_EVENT
    if not endpoint.is_active:
        delivery.status = WebhookDelivery.Status.FAILED
        delivery.last_error = _("El webhook está desactivado.")
        delivery.next_attempt_at = None
        delivery.save(update_fields=["status", "last_error", "next_attempt_at", "updated_at"])
        return None

    body = encode_payload(delivery.payload)
    timestamp = int(time.time())
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "GALBUST-CMMS-Webhooks/1",
        "X-Galbust-Event": delivery.event_type,
        "X-Galbust-Event-Id": str(delivery.event_id),
        SIGNATURE_HEADER: f"t={timestamp},v1={sign(endpoint.secret, timestamp, body)}",
    }
    code, error, retryable = None, "", True
    try:
        address = public_address(endpoint.url)
        code = http_post(endpoint.url, address=address, body=body, headers=headers)
    except UnsafeURLError as exc:
        error, retryable = exc.message, False
    except (OSError, http.client.HTTPException) as exc:
        error = str(exc) or type(exc).__name__
    succeeded = code is not None and 200 <= code < 300
    if code is not None and not succeeded:
        error = f"HTTP {code}"

    delivery.attempts += 1
    delivery.last_attempt_at = now
    delivery.response_status = code
    delivery.last_error = error[:300]
    delay = None
    if succeeded:
        delivery.status = WebhookDelivery.Status.SUCCEEDED
        delivery.delivered_at = now
    elif retryable and not is_test and settings.WEBHOOKS_USE_QUEUE and delivery.attempts < MAX_ATTEMPTS:
        delay = BACKOFF_SECONDS[min(delivery.attempts, len(BACKOFF_SECONDS)) - 1]
    else:
        delivery.status = WebhookDelivery.Status.FAILED
    delivery.next_attempt_at = now + datetime.timedelta(seconds=delay) if delay else None
    with transaction.atomic():
        delivery.save()
        if not is_test:
            if succeeded and endpoint.consecutive_failures:
                WebhookEndpoint.objects.filter(pk=endpoint.pk).update(consecutive_failures=0)
                endpoint.consecutive_failures = 0
            elif delivery.status == WebhookDelivery.Status.FAILED:
                _register_failure(endpoint=endpoint, now=now)
    if delay:
        schedule_delivery(delivery=delivery, delay=delay)
    return delay


def _register_failure(*, endpoint: WebhookEndpoint, now) -> None:
    """Suma una entrega agotada; al llegar al límite desactiva el destino y avisa al admin."""
    from apps.notifications.services import notify_webhook_disabled

    locked = WebhookEndpoint.objects.select_for_update().get(pk=endpoint.pk)
    locked.consecutive_failures += 1
    fields = ["consecutive_failures", "updated_at"]
    if locked.is_active and locked.consecutive_failures >= DISABLE_AFTER_FAILURES:
        locked.is_active = False
        locked.disabled_at = now
        fields += ["is_active", "disabled_at"]
        audit.record(
            action=AuditEvent.Action.DEACTIVATE,
            target=locked,
            changes={"is_active": [True, False], "consecutive_failures": locked.consecutive_failures},
        )
        notify_webhook_disabled(endpoint=locked)
    locked.save(update_fields=fields)
    endpoint.consecutive_failures, endpoint.is_active = locked.consecutive_failures, locked.is_active
    endpoint.disabled_at = locked.disabled_at
