"""Webhooks salientes (X-08, ADR-030): alta de destinos, disparo de eventos y entrega firmada.

Los servicios de negocio llaman a `emit_event` dentro de su transacción; el evento se
arma en `transaction.on_commit` (si la escritura se revierte, no sale nada) y crea una
entrega por destino activo suscrito. Con `WEBHOOKS_USE_QUEUE` la envía el worker con
reintentos y backoff; si no, se intenta una vez al confirmar (demo sin worker).

Contra SSRF: solo `https`, y el dominio debe resolver a direcciones públicas, al guardar
y en cada envío; la conexión va a la dirección ya validada (sin re-resolver) y no se
siguen redirecciones.
"""

import contextlib
import contextvars
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

from .models import EVENT_MODULES, RESPONSE_BODY_LIMIT, TEST_EVENT, Event, WebhookDelivery, WebhookEndpoint

MODULE = "api"  # los webhooks van con la API (Profesional y Empresa, ADR-030)
SECRET_PREFIX = "whsec_"
SIGNATURE_HEADER = "X-Galbust-Signature"
TIMEOUT_SECONDS = 5
MAX_ATTEMPTS = 5
# Espera antes del intento 2, 3, 4 y 5 (segundos): 1 min, 5 min, 30 min, 2 h.
BACKOFF_SECONDS = (60, 300, 1800, 7200)
DISABLE_AFTER_FAILURES = 5  # entregas agotadas seguidas que desactivan el destino
BLOCKED_HOST_SUFFIXES = (".localhost", ".local", ".internal", ".lan", ".home.arpa")
# Sin worker (demo), entregas que se intentan en la misma petición; el resto queda fallida.
SYNC_DELIVERY_LIMIT = 20
# Eventos por tarea `webhooks.dispatch_events` al despachar un lote de importación con cola.
BATCH_TASK_SIZE = 500
# Antigüedad desde la que se purgan las entregas (tarea diaria y `purge_webhook_deliveries`).
DELIVERY_RETENTION_DAYS = 90


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


def http_post(url: str, *, address: str, body: bytes, headers: dict) -> tuple[int, str]:
    """POST con timeout corto; devuelve el código HTTP y el inicio de la respuesta (sin redirecciones)."""
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
        text = response.read(4096).decode("utf-8", errors="replace")
        return response.status, text[:RESPONSE_BODY_LIMIT]
    finally:
        connection.close()


def sign(secret: str, timestamp: int, body: bytes) -> str:
    """HMAC-SHA256 en hex de `"<timestamp>.<cuerpo>"` con el secreto del destino."""
    return hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()


def encode_payload(payload: dict) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()


# --- Destinos ---------------------------------------------------------------------------------


def _clean_events(events, *, keep=()) -> list[str]:
    """Eventos conocidos que el plan ofrece; `keep` conserva los ya suscritos aunque no se ofrezcan."""
    allowed = set(enabled_events(require_current_organization())) | set(keep)
    events = [code for code in Event.values if code in set(events) and code in allowed]
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
    endpoint.name, endpoint.url = name.strip(), url
    endpoint.events = _clean_events(events, keep=endpoint.events)
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

# Eventos de una importación masiva agrupados (`collect_events`); None fuera de una.
_batch: contextvars.ContextVar[list | None] = contextvars.ContextVar("webhook_batch", default=None)


def emit_event(*, event_type: str, instance, change: dict | None = None) -> None:
    """Programa el evento para cuando la transacción se confirme (sin consultas antes).

    `change` va tal cual en el cuerpo (p. ej. `{"from": "planned", "to": "in_progress"}`).
    Dentro de `collect_events` el evento se suma al lote en vez de programarse solo.
    """
    organization = require_current_organization()
    event = (str(event_type), type(instance)._meta.label, str(instance.pk), change)
    batch = _batch.get()
    if batch is not None:
        batch.append(event)
        return

    def run():
        with tenant_context(organization):
            dispatch_events([event])

    transaction.on_commit(run, robust=True)


@contextlib.contextmanager
def collect_events():
    """Importaciones masivas: un solo despacho al confirmar en vez de uno por fila.

    Con cola, el lote va al worker (`webhooks.dispatch_events`, en trozos de
    `BATCH_TASK_SIZE`): la importación no espera la creación de entregas ni los envíos.
    Una fila revertida no envía nada (su objeto ya no existe al despachar).
    """
    if _batch.get() is not None:  # anidado: lo despacha el lote de afuera
        yield
        return
    events: list = []
    token = _batch.set(events)
    try:
        yield
    finally:
        _batch.reset(token)
    if events:
        organization = require_current_organization()
        transaction.on_commit(lambda: _dispatch_batch(organization, events), robust=True)


def _dispatch_batch(organization, events: list) -> None:
    if settings.WEBHOOKS_USE_QUEUE:
        from .tasks import dispatch_events_task

        for start in range(0, len(events), BATCH_TASK_SIZE):
            chunk = [list(event) for event in events[start : start + BATCH_TASK_SIZE]]
            dispatch_events_task.defer(organization_id=str(organization.pk), events=chunk)
        return
    with tenant_context(organization):
        dispatch_events(events)


def enabled_events(organization) -> list[str]:
    """Eventos que el plan permite (módulo `api` y el módulo de cada evento)."""
    if not has_module(organization, MODULE):
        return []
    return [code for code in Event.values if has_module(organization, EVENT_MODULES[code])]


def _sources():
    """Modelo → (consulta, serializador de la API v1): `data` con los mismos campos que la API."""
    from apps.api import serializers as s
    from apps.assets.models import Asset, MeterReading
    from apps.condition.models import ConditionAlert
    from apps.condition.selectors import ALERT_RELATED
    from apps.inventory.models import StockItem, StockMovement
    from apps.work.models import WorkOrder, WorkRequest

    return {
        WorkOrder._meta.label: (WorkOrder.objects.all(), s.WorkOrderSerializer),
        WorkRequest._meta.label: (WorkRequest.objects.select_related("work_order"), s.WorkRequestSerializer),
        Asset._meta.label: (Asset.objects.select_related("asset_class", "location"), s.AssetSerializer),
        MeterReading._meta.label: (MeterReading.objects.all(), s.MeterReadingSerializer),
        ConditionAlert._meta.label: (
            ConditionAlert.objects.select_related(*ALERT_RELATED),
            s.ConditionAlertSerializer,
        ),
        StockItem._meta.label: (StockItem.objects.select_related("part", "warehouse"), s.StockItemSerializer),
        StockMovement._meta.label: (
            StockMovement.objects.select_related("part", "warehouse"),
            s.StockMovementSerializer,
        ),
    }


def _load_data(events) -> dict:
    """(modelo, pk) → `data` serializado, con una consulta por modelo. Lo que no existe falta."""
    from rest_framework.utils.encoders import JSONEncoder

    sources = _sources()
    wanted: dict[str, set] = {}
    for _type, label, pk, _change in events:
        wanted.setdefault(label, set()).add(pk)
    data = {}
    # Costos visibles: el token de servicio (rol planificador) también los ve en la API.
    context = {"show_costs": True}
    for label, pks in wanted.items():
        queryset, serializer = sources[label]
        for pk, instance in queryset.in_bulk(list(pks)).items():
            # Mismos campos y formato que la API v1 (UUID, fechas y decimales como texto).
            serialized = serializer(instance, context=context).data
            data[(label, str(pk))] = json.loads(json.dumps(serialized, cls=JSONEncoder))
    return data


def build_payload(*, event_type: str, data: dict, change: dict | None = None) -> dict:
    organization = require_current_organization()
    payload = {
        "id": str(uuid.uuid4()),
        "type": event_type,
        "created_at": timezone.now().astimezone(datetime.UTC).isoformat().replace("+00:00", "Z"),
        "organization": {"id": str(organization.pk), "slug": organization.slug, "name": organization.name},
        "data": data,
    }
    if change:
        payload["change"] = change
    return payload


def dispatch_events(events) -> list[WebhookDelivery]:
    """Crea y programa una entrega por evento y destino activo suscrito (en orden de emisión)."""
    organization = require_current_organization()
    allowed = set(enabled_events(organization))
    if not any(event[0] in allowed for event in events):
        return []
    # El filtro por evento va en Python: pocos destinos por organización y sirve en SQLite.
    endpoints = list(WebhookEndpoint.objects.filter(is_active=True))
    subscribed = {code: [e for e in endpoints if code in e.events] for code in allowed}
    events = [event for event in events if subscribed.get(event[0])]
    if not events:
        return []
    data = _load_data(events)
    rows, targets = [], []
    for event_type, label, pk, change in events:
        if (label, pk) not in data:
            continue  # revertido antes de confirmar
        payload = build_payload(event_type=event_type, data=data[(label, pk)], change=change)
        for endpoint in subscribed[event_type]:
            rows.append(
                WebhookDelivery(
                    organization=organization,
                    endpoint=endpoint,
                    event_id=payload["id"],
                    event_type=event_type,
                    payload=payload,
                )
            )
            targets.append(endpoint)
    deliveries = WebhookDelivery.objects.bulk_create(rows)
    for index, (delivery, endpoint) in enumerate(zip(deliveries, targets, strict=True)):
        delivery.endpoint = endpoint
        if settings.WEBHOOKS_USE_QUEUE or index < SYNC_DELIVERY_LIMIT:
            schedule_delivery(delivery=delivery)
        else:
            _skip_without_worker(delivery)
    return deliveries


def _skip_without_worker(delivery: WebhookDelivery) -> None:
    """Sin worker, un lote grande no se envía en la petición: queda fallida para reenviarla."""
    delivery.status = WebhookDelivery.Status.FAILED
    delivery.last_error = _("No se envió: sin worker solo se envían %(limit)s entregas por lote.") % {
        "limit": SYNC_DELIVERY_LIMIT
    }
    delivery.save(update_fields=["status", "last_error", "updated_at"])


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


@transaction.atomic
def redeliver(*, membership, delivery: WebhookDelivery) -> WebhookDelivery:
    """Reenvía a mano una entrega fallida: otra entrega con el mismo cuerpo y el mismo `event_id`.

    El receptor la reconoce como el mismo evento (idempotencia por `X-Galbust-Event-Id`).
    La original queda como estaba; el reenvío se audita.
    """
    require_perm(membership, Perm.API_TOKEN_MANAGE)
    endpoint = delivery.endpoint
    if delivery.status != WebhookDelivery.Status.FAILED:
        raise WebhookError(_("Solo se reenvía una entrega fallida."))
    if delivery.event_type == TEST_EVENT:
        raise WebhookError(_("Una prueba no se reenvía: usa «Enviar prueba»."))
    if not endpoint.is_active:
        raise WebhookError(_("Activa el webhook antes de reenviar."))
    copy = WebhookDelivery.objects.create(
        endpoint=endpoint,
        event_id=delivery.event_id,
        event_type=delivery.event_type,
        payload=delivery.payload,
        is_redelivery=True,
        redelivery_of=delivery,
        created_by=membership.user,
    )
    audit.record(
        action=AuditEvent.Action.CREATE,
        target=copy,
        membership=membership,
        changes={"redelivery_of": str(delivery.pk), "event_id": str(delivery.event_id)},
    )
    organization = require_current_organization()

    def run():
        with tenant_context(organization):
            schedule_delivery(delivery=copy)

    transaction.on_commit(run, robust=True)
    return copy


@transaction.atomic
def regenerate_secret(*, membership, endpoint: WebhookEndpoint) -> str:
    """Nuevo secreto de firma; el anterior deja de valer de inmediato (sin ventana de traslape).

    Las entregas siguientes, incluidos los reintentos pendientes, se firman con el nuevo.
    Se audita sin el valor del secreto.
    """
    require_perm(membership, Perm.API_TOKEN_MANAGE)
    endpoint = WebhookEndpoint.objects.select_for_update().get(pk=endpoint.pk)
    endpoint.secret = SECRET_PREFIX + secrets.token_urlsafe(32)
    endpoint.secret_rotated_at = timezone.now()
    endpoint.save(update_fields=["secret", "secret_rotated_at", "updated_at"])
    audit.record(
        action=AuditEvent.Action.UPDATE,
        target=endpoint,
        membership=membership,
        changes={"signature_rotated_at": endpoint.secret_rotated_at},
    )
    return endpoint.secret


def purge_old_deliveries(*, days: int = DELIVERY_RETENTION_DAYS, now=None) -> int:
    """Borra las entregas terminadas de más de `days` días de la organización del contexto.

    Las pendientes (con reintentos por delante) no se tocan. Es un registro operativo, no
    historial del negocio: se borra de verdad (docs/07). Devuelve cuántas se borraron.
    """
    require_current_organization()
    cutoff = (now or timezone.now()) - datetime.timedelta(days=days)
    old = WebhookDelivery.objects.filter(created_at__lt=cutoff).exclude(status=WebhookDelivery.Status.PENDING)
    # Primero desenganchar los reenvíos que apuntan a filas que se borran (SET_NULL fila a fila).
    WebhookDelivery.objects.filter(redelivery_of__in=old.values("pk")).update(redelivery_of=None)
    deleted, _detail = old.delete()
    return deleted


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
    code, text, error, retryable = None, "", "", True
    try:
        address = public_address(endpoint.url)
        code, text = http_post(endpoint.url, address=address, body=body, headers=headers)
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
    delivery.response_body = text[:RESPONSE_BODY_LIMIT]
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
