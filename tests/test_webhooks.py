"""Webhooks salientes (X-08, ADR-030): firma, disparo, reintentos, SSRF y desactivación.

Sin red: se reemplazan `resolve_host` (DNS) y `http_post` (cliente HTTP), nunca el ORM.
"""

import hashlib
import hmac
import json

import pytest
from django.urls import reverse

from apps.audit.models import AuditEvent
from apps.core.tenancy import tenant_context
from apps.notifications.models import Kind, Notification
from apps.organizations.models import Plan
from apps.webhooks import services
from apps.webhooks.models import TEST_EVENT, Event, WebhookDelivery, WebhookEndpoint
from apps.work.services import create_work_order, report_failure, transition
from tests.conftest import login

PUBLIC_IP = "93.184.216.34"


@pytest.fixture
def network(monkeypatch):
    """DNS falso (todo resuelve a una IP pública) y cliente HTTP que registra cada envío."""
    state = {"addresses": {}, "status": 200, "body": "ok", "error": None, "calls": []}

    def resolve(host, port):
        return state["addresses"].get(host, [PUBLIC_IP])

    def post(url, *, address, body, headers):
        state["calls"].append({"url": url, "address": address, "body": body, "headers": headers})
        if state["error"]:
            raise state["error"]
        return state["status"], state["body"]

    monkeypatch.setattr(services, "resolve_host", resolve)
    monkeypatch.setattr(services, "http_post", post)
    return state


def _endpoint(tenant, events=(Event.WORK_ORDER_CREATED,), url="https://erp.example.com/hooks"):
    with tenant_context(tenant.org):
        return services.create_endpoint(membership=tenant.admin, name="ERP", url=url, events=list(events))


def _work_order(tenant):
    return create_work_order(work_type="corrective", title="Fuga", asset=tenant.asset, by=tenant.admin.user)


# --- Alta y SSRF al guardar ------------------------------------------------------------------


def test_create_endpoint_generates_secret_and_audits_without_it(tenant_a, network):
    endpoint = _endpoint(tenant_a)
    assert endpoint.secret.startswith("whsec_") and len(endpoint.secret) > 40
    with tenant_context(tenant_a.org):
        event = AuditEvent.objects.get(action=AuditEvent.Action.CREATE, target_id=str(endpoint.pk))
    assert "secret" not in event.changes and event.changes["url"] == endpoint.url


@pytest.mark.parametrize(
    "url,addresses",
    [
        ("http://erp.example.com/hooks", None),  # sin TLS
        ("https://localhost/hooks", None),
        ("https://127.0.0.1/hooks", ["127.0.0.1"]),
        ("https://10.0.0.5/hooks", ["10.0.0.5"]),
        ("https://metadata.example.com/", ["169.254.169.254"]),  # link-local (metadatos de la nube)
        ("https://v6.example.com/", ["::1"]),
        ("https://mixed.example.com/", [PUBLIC_IP, "192.168.1.10"]),  # una sola privada basta
        ("https://printer.local/", None),
    ],
)
def test_private_or_insecure_urls_are_rejected_on_save(tenant_a, network, url, addresses):
    from urllib.parse import urlsplit

    if addresses:
        network["addresses"][urlsplit(url).hostname] = addresses
    with pytest.raises(services.UnsafeURLError), tenant_context(tenant_a.org):
        services.create_endpoint(
            membership=tenant_a.admin, name="x", url=url, events=[Event.WORK_ORDER_CREATED]
        )
    with tenant_context(tenant_a.org):
        assert not WebhookEndpoint.objects.exists()


def test_create_requires_at_least_one_known_event(tenant_a, network):
    with pytest.raises(services.WebhookError), tenant_context(tenant_a.org):
        services.create_endpoint(
            membership=tenant_a.admin, name="x", url="https://erp.example.com", events=["asset.deleted"]
        )


# --- Disparo y firma ----------------------------------------------------------------------------


def test_work_order_created_is_signed_and_sent_after_commit(
    tenant_a, network, django_capture_on_commit_callbacks
):
    endpoint = _endpoint(tenant_a)
    with tenant_context(tenant_a.org), django_capture_on_commit_callbacks(execute=True):
        work_order = _work_order(tenant_a)
    assert len(network["calls"]) == 1
    call = network["calls"][0]
    assert call["address"] == PUBLIC_IP
    payload = json.loads(call["body"])
    assert payload["type"] == Event.WORK_ORDER_CREATED
    assert payload["created_at"].endswith("Z")
    assert payload["organization"] == {
        "id": str(tenant_a.org.pk),
        "slug": tenant_a.org.slug,
        "name": "Empresa-A",
    }
    assert payload["data"]["id"] == str(work_order.pk) and payload["data"]["code"] == work_order.code
    assert call["headers"]["X-Galbust-Event-Id"] == payload["id"]
    # Verificación tal como la documenta docs/08_api.md.
    parts = dict(item.split("=", 1) for item in call["headers"]["X-Galbust-Signature"].split(","))
    expected = hmac.new(
        endpoint.secret.encode(), f"{parts['t']}.".encode() + call["body"], hashlib.sha256
    ).hexdigest()
    assert hmac.compare_digest(parts["v1"], expected)
    with tenant_context(tenant_a.org):
        delivery = WebhookDelivery.objects.get()
    assert delivery.status == WebhookDelivery.Status.SUCCEEDED
    assert delivery.attempts == 1 and delivery.response_status == 200
    assert str(delivery.event_id) == payload["id"]


def test_rollback_sends_nothing(tenant_a, network, django_capture_on_commit_callbacks):
    from django.db import transaction

    _endpoint(tenant_a)
    with tenant_context(tenant_a.org), django_capture_on_commit_callbacks(execute=True):
        with pytest.raises(RuntimeError), transaction.atomic():
            _work_order(tenant_a)
            raise RuntimeError
    assert network["calls"] == []


def test_only_subscribed_active_endpoints_receive_events(
    tenant_a, network, django_capture_on_commit_callbacks
):
    created = _endpoint(tenant_a, events=[Event.WORK_ORDER_CREATED])
    requests = _endpoint(tenant_a, events=[Event.WORK_REQUEST_CREATED, Event.WORK_ORDER_COMPLETED])
    inactive = _endpoint(tenant_a, events=[Event.WORK_REQUEST_CREATED])
    with tenant_context(tenant_a.org):
        services.update_endpoint(
            membership=tenant_a.admin,
            endpoint=inactive,
            name=inactive.name,
            url=inactive.url,
            events=inactive.events,
            is_active=False,
        )
        with django_capture_on_commit_callbacks(execute=True):
            report_failure(asset=tenant_a.asset, description="Ruido", membership=tenant_a.admin)
        deliveries = set(WebhookDelivery.objects.values_list("endpoint_id", "event_type"))
    # El aviso crea la solicitud y su OT correctiva: cada destino recibe solo lo suscrito.
    assert (requests.pk, Event.WORK_REQUEST_CREATED) in deliveries
    assert {event for endpoint_id, event in deliveries if endpoint_id == created.pk} <= {
        Event.WORK_ORDER_CREATED
    }
    assert inactive.pk not in {endpoint_id for endpoint_id, _ in deliveries}
    assert len(network["calls"]) == len(deliveries)


def test_work_order_completed_event(tenant_a, network, django_capture_on_commit_callbacks):
    _endpoint(tenant_a, events=[Event.WORK_ORDER_COMPLETED])
    with tenant_context(tenant_a.org):
        work_order = _work_order(tenant_a)
        transition(work_order=work_order, to_status="in_progress", membership=tenant_a.admin)
        with django_capture_on_commit_callbacks(execute=True):
            transition(work_order=work_order, to_status="completed", membership=tenant_a.admin)
    assert [json.loads(c["body"])["type"] for c in network["calls"]] == [Event.WORK_ORDER_COMPLETED]


def test_plan_without_api_module_sends_nothing(tenant_a, network, django_capture_on_commit_callbacks):
    _endpoint(tenant_a)
    tenant_a.org.plan = Plan.objects.create(code="mini", name="Mini", features=["work_orders", "assets"])
    tenant_a.org.save()
    with tenant_context(tenant_a.org), django_capture_on_commit_callbacks(execute=True):
        _work_order(tenant_a)
        assert not WebhookDelivery.objects.exists()
    assert network["calls"] == []


# --- Reintentos, SSRF al enviar y desactivación ------------------------------------------------


@pytest.fixture
def queue(settings, monkeypatch):
    """Con cola: registra lo que se encolaría en vez de hablar con Procrastinate."""
    settings.WEBHOOKS_USE_QUEUE = True
    enqueued = []
    monkeypatch.setattr(
        services, "_enqueue", lambda *, delivery, delay: enqueued.append((delivery.pk, delay))
    )
    return enqueued


def _pending_delivery(tenant, endpoint):
    with tenant_context(tenant.org):
        payload = services.build_payload(event_type=Event.WORK_ORDER_CREATED, data={"id": "x"})
        return WebhookDelivery.objects.create(
            endpoint=endpoint, event_id=payload["id"], event_type=payload["type"], payload=payload
        )


def test_failures_retry_with_backoff_then_give_up(tenant_a, network, queue):
    endpoint = _endpoint(tenant_a)
    delivery = _pending_delivery(tenant_a, endpoint)
    network["status"] = 503
    with tenant_context(tenant_a.org):
        delays = [services.attempt_delivery(delivery=delivery) for _ in range(services.MAX_ATTEMPTS)]
        delivery.refresh_from_db()
        endpoint.refresh_from_db()
    assert delays == [*services.BACKOFF_SECONDS, None]
    assert [delay for _, delay in queue] == list(services.BACKOFF_SECONDS)
    assert delivery.status == WebhookDelivery.Status.FAILED and delivery.attempts == services.MAX_ATTEMPTS
    assert delivery.last_error == "HTTP 503" and delivery.next_attempt_at is None
    assert endpoint.consecutive_failures == 1 and endpoint.is_active


def test_network_error_is_retried_and_success_resets_failures(tenant_a, network, queue):
    endpoint = _endpoint(tenant_a)
    with tenant_context(tenant_a.org):
        WebhookEndpoint.objects.filter(pk=endpoint.pk).update(consecutive_failures=3)
        endpoint.refresh_from_db()
    delivery = _pending_delivery(tenant_a, endpoint)
    network["error"] = TimeoutError("timed out")
    with tenant_context(tenant_a.org):
        assert services.attempt_delivery(delivery=delivery) == services.BACKOFF_SECONDS[0]
        assert delivery.last_error == "timed out" and delivery.next_attempt_at is not None
        network["error"] = None
        assert services.attempt_delivery(delivery=delivery) is None
        endpoint.refresh_from_db()
    assert delivery.status == WebhookDelivery.Status.SUCCEEDED and delivery.attempts == 2
    assert endpoint.consecutive_failures == 0


def test_private_address_at_send_time_fails_without_request_or_retry(tenant_a, network, queue):
    """DNS rebinding: el dominio era público al guardar y apunta a la red interna al enviar."""
    endpoint = _endpoint(tenant_a)
    delivery = _pending_delivery(tenant_a, endpoint)
    network["addresses"]["erp.example.com"] = ["10.1.2.3"]
    with tenant_context(tenant_a.org):
        assert services.attempt_delivery(delivery=delivery) is None
    assert network["calls"] == [] and queue == []
    assert delivery.status == WebhookDelivery.Status.FAILED and "privada" in delivery.last_error


def test_consecutive_failures_disable_endpoint_and_notify_admin(
    tenant_a, network, django_capture_on_commit_callbacks
):
    endpoint = _endpoint(tenant_a)
    network["status"] = 500
    with tenant_context(tenant_a.org):
        with django_capture_on_commit_callbacks(execute=True):
            for _ in range(services.DISABLE_AFTER_FAILURES):
                # Sin cola (por defecto en tests): un intento por evento y queda fallido.
                services.attempt_delivery(delivery=_pending_delivery(tenant_a, endpoint))
        endpoint.refresh_from_db()
        assert not endpoint.is_active and endpoint.disabled_at is not None
        notification = Notification.objects.get(kind=Kind.WEBHOOK_DISABLED)
        assert notification.recipient == tenant_a.admin.user
        assert AuditEvent.objects.filter(
            action=AuditEvent.Action.DEACTIVATE, target_id=str(endpoint.pk)
        ).exists()
        # Un destino desactivado no recibe eventos nuevos.
        network["calls"].clear()
        with django_capture_on_commit_callbacks(execute=True):
            _work_order(tenant_a)
    assert network["calls"] == []
    # Reactivarlo reinicia el contador.
    with tenant_context(tenant_a.org):
        services.update_endpoint(
            membership=tenant_a.admin,
            endpoint=endpoint,
            name=endpoint.name,
            url=endpoint.url,
            events=endpoint.events,
            is_active=True,
        )
        endpoint.refresh_from_db()
    assert endpoint.consecutive_failures == 0 and endpoint.disabled_at is None


def test_send_test_event_does_not_count_as_failure(tenant_a, network, django_capture_on_commit_callbacks):
    endpoint = _endpoint(tenant_a)
    network["status"] = 404
    with tenant_context(tenant_a.org), django_capture_on_commit_callbacks(execute=True):
        delivery = services.send_test(membership=tenant_a.admin, endpoint=endpoint)
    with tenant_context(tenant_a.org):  # con RLS (CI) las filas solo se ven en contexto
        delivery.refresh_from_db()
        endpoint.refresh_from_db()
    assert delivery.event_type == TEST_EVENT and delivery.status == WebhookDelivery.Status.FAILED
    assert delivery.response_status == 404 and endpoint.consecutive_failures == 0


# --- Pantalla -----------------------------------------------------------------------------------


def test_admin_creates_edits_and_tests_from_settings(
    client, tenant_a, network, django_capture_on_commit_callbacks
):
    login(client, tenant_a.admin)
    response = client.post(
        reverse("webhook_list"),
        {"name": "ERP", "url": "https://erp.example.com/in", "events": [Event.WORK_ORDER_CREATED]},
    )
    with tenant_context(tenant_a.org):
        endpoint = WebhookEndpoint.objects.get()
    assert response.status_code == 302 and response.url == reverse("webhook_detail", args=[endpoint.pk])
    page = client.get(response.url).content.decode()
    assert endpoint.secret in page
    with django_capture_on_commit_callbacks(execute=True):
        client.post(reverse("webhook_test", args=[endpoint.pk]))
    assert json.loads(network["calls"][0]["body"])["type"] == TEST_EVENT
    client.post(
        reverse("webhook_detail", args=[endpoint.pk]),
        {"name": "ERP 2", "url": endpoint.url, "events": [Event.WORK_ORDER_COMPLETED]},
    )
    with tenant_context(tenant_a.org):
        endpoint.refresh_from_db()
    assert endpoint.name == "ERP 2" and not endpoint.is_active
    assert endpoint.events == [Event.WORK_ORDER_COMPLETED]


def test_screen_shows_ssrf_error_on_the_url_field(client, tenant_a, network):
    network["addresses"]["intranet.example.com"] = ["192.168.0.2"]
    login(client, tenant_a.admin)
    response = client.post(
        reverse("webhook_list"),
        {"name": "x", "url": "https://intranet.example.com/", "events": [Event.WORK_ORDER_CREATED]},
    )
    assert response.status_code == 200
    assert "privada" in response.content.decode()
