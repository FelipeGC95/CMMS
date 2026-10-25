"""Gestión de webhooks (X-08, F3): regenerar secreto, reenviar, ver el cuerpo y purgar entregas."""

import datetime
import hashlib
import hmac
import json

import pytest
from django.core.management import call_command
from django.urls import reverse
from django.utils import timezone

from apps.audit.models import AuditEvent
from apps.core.tenancy import tenant_context
from apps.webhooks import services
from apps.webhooks.models import Event, WebhookDelivery
from apps.work.services import create_work_order
from tests.conftest import login

pytestmark = pytest.mark.django_db


@pytest.fixture
def network(monkeypatch):
    state = {"status": 500, "body": "boom", "calls": []}
    monkeypatch.setattr(services, "resolve_host", lambda host, port: ["93.184.216.34"])

    def post(url, *, address, body, headers):
        state["calls"].append({"body": body, "headers": headers})
        return state["status"], state["body"]

    monkeypatch.setattr(services, "http_post", post)
    return state


@pytest.fixture
def endpoint(tenant_a, network):
    with tenant_context(tenant_a.org):
        return services.create_endpoint(
            membership=tenant_a.admin,
            name="ERP",
            url="https://erp.example.com/hooks",
            events=[Event.WORK_ORDER_CREATED],
        )


@pytest.fixture
def failed(tenant_a, endpoint, network, django_capture_on_commit_callbacks):
    """Una entrega de `work_order.created` que falló (HTTP 500, sin worker: un solo intento)."""
    with tenant_context(tenant_a.org):
        with django_capture_on_commit_callbacks(execute=True):
            create_work_order(work_type="corrective", title="Fuga", asset=tenant_a.asset)
        return WebhookDelivery.objects.select_related("endpoint").get()


def _signatures(call):
    """(t, [v1, …]) de la cabecera, leída como lista (puede traer dos `v1`)."""
    items = [item.split("=", 1) for item in call["headers"]["X-Galbust-Signature"].split(",")]
    return dict(items)["t"], [value for key, value in items if key == "v1"]


def _verify(secret, call):
    """Como el ejemplo de docs/08: acepta si alguna `v1` coincide."""
    timestamp, received = _signatures(call)
    expected = hmac.new(secret.encode(), f"{timestamp}.".encode() + call["body"], hashlib.sha256).hexdigest()
    return any(hmac.compare_digest(value, expected) for value in received)


def _send(tenant, network, capture):
    network["status"] = 200
    with tenant_context(tenant.org):
        with capture(execute=True):
            create_work_order(work_type="corrective", title="Fuga", asset=tenant.asset)
    return network["calls"][-1]


# --- Regenerar el secreto -----------------------------------------------------------------------


def test_without_rotation_there_is_a_single_signature(
    tenant_a, endpoint, network, django_capture_on_commit_callbacks
):
    call = _send(tenant_a, network, django_capture_on_commit_callbacks)
    assert len(_signatures(call)[1]) == 1 and _verify(endpoint.secret, call)


def test_regenerate_keeps_the_old_secret_signing_for_24_hours(
    tenant_a, endpoint, network, django_capture_on_commit_callbacks
):
    """P-07: en la ventana de traslape cada entrega lleva dos `v1`: la anterior y, al final, la nueva."""
    old = endpoint.secret
    with tenant_context(tenant_a.org):
        new = services.regenerate_secret(membership=tenant_a.admin, endpoint=endpoint)
        endpoint.refresh_from_db()
        event = AuditEvent.objects.get(action=AuditEvent.Action.UPDATE, target_id=str(endpoint.pk))
    assert new != old and new.startswith("whsec_")
    assert endpoint.previous_secret == old
    window = endpoint.previous_secret_expires_at - endpoint.secret_rotated_at
    assert window == services.SECRET_OVERLAP == datetime.timedelta(hours=24)
    assert "signature_rotated_at" in event.changes and "previous_signature_valid_until" in event.changes
    assert new not in json.dumps(event.changes) and old not in json.dumps(event.changes)

    call = _send(tenant_a, network, django_capture_on_commit_callbacks)
    _timestamp, signatures = _signatures(call)
    assert len(signatures) == 2 and _verify(new, call) and _verify(old, call)
    # Leída como diccionario (último `v1`) la cabecera da la firma del secreto nuevo.
    parts = dict(item.split("=", 1) for item in call["headers"]["X-Galbust-Signature"].split(","))
    assert _verify(new, {**call, "headers": {"X-Galbust-Signature": f"t={parts['t']},v1={parts['v1']}"}})


def test_old_secret_stops_signing_when_the_window_ends(
    tenant_a, endpoint, network, django_capture_on_commit_callbacks, monkeypatch
):
    old = endpoint.secret
    with tenant_context(tenant_a.org):
        new = services.regenerate_secret(membership=tenant_a.admin, endpoint=endpoint)
    later = timezone.now() + services.SECRET_OVERLAP + datetime.timedelta(seconds=1)
    monkeypatch.setattr(timezone, "now", lambda: later)
    call = _send(tenant_a, network, django_capture_on_commit_callbacks)
    assert _signatures(call)[1] and _verify(new, call) and not _verify(old, call)
    # La purga diaria lo saca de la base.
    with tenant_context(tenant_a.org):
        assert services.clear_expired_previous_secrets() == 1
        endpoint.refresh_from_db()
    assert endpoint.previous_secret == "" and endpoint.previous_secret_expires_at is None


def test_regenerate_twice_keeps_only_the_latest_previous(tenant_a, endpoint):
    first = endpoint.secret
    with tenant_context(tenant_a.org):
        second = services.regenerate_secret(membership=tenant_a.admin, endpoint=endpoint)
        services.regenerate_secret(membership=tenant_a.admin, endpoint=endpoint)
        endpoint.refresh_from_db()
    assert endpoint.previous_secret == second != first


def test_leaked_secret_is_invalidated_at_once(
    tenant_a, endpoint, network, django_capture_on_commit_callbacks
):
    old = endpoint.secret
    with tenant_context(tenant_a.org):
        new = services.regenerate_secret(membership=tenant_a.admin, endpoint=endpoint, keep_previous=False)
        endpoint.refresh_from_db()
    assert endpoint.previous_secret == "" and endpoint.previous_secret_expires_at is None
    call = _send(tenant_a, network, django_capture_on_commit_callbacks)
    assert _verify(new, call) and not _verify(old, call)


def test_discard_previous_secret_ends_the_window(
    tenant_a, endpoint, network, django_capture_on_commit_callbacks
):
    old = endpoint.secret
    with tenant_context(tenant_a.org):
        services.regenerate_secret(membership=tenant_a.admin, endpoint=endpoint)
        services.discard_previous_secret(membership=tenant_a.admin, endpoint=endpoint)
        endpoint.refresh_from_db()
        audited = AuditEvent.objects.filter(
            action=AuditEvent.Action.UPDATE, target_id=str(endpoint.pk)
        ).count()
    assert endpoint.previous_secret == "" and audited == 2
    assert not _verify(old, _send(tenant_a, network, django_capture_on_commit_callbacks))


def test_regenerate_view_shows_the_new_secret_and_the_window(client, tenant_a, endpoint):
    login(client, tenant_a.admin)
    response = client.post(reverse("webhook_secret", args=[endpoint.pk]))
    with tenant_context(tenant_a.org):
        endpoint.refresh_from_db()
    html = response.content.decode()
    assert response.status_code == 200 and endpoint.secret in html
    assert "cada entrega lleva también la firma del anterior" in html
    assert reverse("webhook_previous_secret", args=[endpoint.pk]) in html
    assert endpoint.secret_rotated_at is not None and endpoint.previous_secret

    response = client.post(reverse("webhook_previous_secret", args=[endpoint.pk]), follow=True)
    with tenant_context(tenant_a.org):
        endpoint.refresh_from_db()
    assert "El secreto anterior dejó de valer." in response.content.decode()
    assert endpoint.previous_secret == ""


def test_regenerate_view_can_revoke_the_old_secret_at_once(client, tenant_a, endpoint):
    login(client, tenant_a.admin)
    response = client.post(reverse("webhook_secret", args=[endpoint.pk]), {"revoke_previous": "1"})
    with tenant_context(tenant_a.org):
        endpoint.refresh_from_db()
    assert "El anterior dejó de valer" in response.content.decode()
    assert endpoint.previous_secret == ""


# --- Reenviar ---------------------------------------------------------------------------------


def test_redeliver_creates_a_new_delivery_with_the_same_event_id(
    tenant_a, failed, network, django_capture_on_commit_callbacks
):
    assert failed.status == WebhookDelivery.Status.FAILED and failed.response_body == "boom"
    network["status"], network["body"] = 200, "recibido"
    with tenant_context(tenant_a.org):
        with django_capture_on_commit_callbacks(execute=True):
            copy = services.redeliver(membership=tenant_a.admin, delivery=failed)
        copy.refresh_from_db()
        failed.refresh_from_db()
        audited = AuditEvent.objects.filter(action=AuditEvent.Action.CREATE, target_id=str(copy.pk)).exists()
    assert copy.pk != failed.pk and copy.event_id == failed.event_id and copy.payload == failed.payload
    assert (
        copy.is_redelivery and copy.redelivery_of_id == failed.pk and copy.created_by == tenant_a.admin.user
    )
    assert copy.status == WebhookDelivery.Status.SUCCEEDED and copy.response_body == "recibido"
    assert failed.status == WebhookDelivery.Status.FAILED  # la original no cambia
    assert network["calls"][-1]["headers"]["X-Galbust-Event-Id"] == str(failed.event_id)
    assert audited


def test_only_failed_non_test_deliveries_are_redelivered(tenant_a, failed, endpoint, network):
    with tenant_context(tenant_a.org):
        WebhookDelivery.objects.filter(pk=failed.pk).update(status=WebhookDelivery.Status.SUCCEEDED)
        failed.refresh_from_db()
        with pytest.raises(services.WebhookError):
            services.redeliver(membership=tenant_a.admin, delivery=failed)
        test = services.send_test(membership=tenant_a.admin, endpoint=endpoint)
        WebhookDelivery.objects.filter(pk=test.pk).update(status=WebhookDelivery.Status.FAILED)
        test.refresh_from_db()
        with pytest.raises(services.WebhookError):
            services.redeliver(membership=tenant_a.admin, delivery=test)


def test_redeliver_view_and_delivery_detail(client, tenant_a, failed, endpoint, network):
    login(client, tenant_a.admin)
    url = reverse("webhook_delivery", args=[endpoint.pk, failed.pk])
    html = client.get(url).content.decode()
    assert str(failed.event_id) in html and "boom" in html
    assert "&quot;type&quot;: &quot;work_order.created&quot;" in html  # cuerpo enviado, escapado
    network["status"] = 200
    response = client.post(reverse("webhook_redeliver", args=[endpoint.pk, failed.pk]))
    with tenant_context(tenant_a.org):
        copy = WebhookDelivery.objects.get(is_redelivery=True)
    assert response.status_code == 302 and response["Location"].endswith(f"/deliveries/{copy.pk}/")
    detail = client.get(reverse("webhook_detail", args=[endpoint.pk])).content.decode()
    assert "Reenvío manual" in detail and url in detail


# --- Purga ------------------------------------------------------------------------------------


def _age(delivery, days):
    WebhookDelivery.objects.filter(pk=delivery.pk).update(
        created_at=timezone.now() - datetime.timedelta(days=days)
    )


def test_purge_deletes_finished_deliveries_older_than_90_days(tenant_a, failed, endpoint, network):
    with tenant_context(tenant_a.org):
        recent = services.send_test(membership=tenant_a.admin, endpoint=endpoint)
        pending = services.send_test(membership=tenant_a.admin, endpoint=endpoint)
        copy = services.redeliver(membership=tenant_a.admin, delivery=failed)
        WebhookDelivery.objects.filter(pk=pending.pk).update(status=WebhookDelivery.Status.PENDING)
        _age(failed, 91)
        _age(pending, 200)
        _age(recent, 89)
        assert services.purge_old_deliveries() == 1
        remaining = set(WebhookDelivery.objects.values_list("pk", flat=True))
        copy.refresh_from_db()
    assert remaining == {recent.pk, pending.pk, copy.pk}
    assert copy.redelivery_of_id is None  # el reenvío reciente se conserva


def test_purge_command_and_periodic_task(tenant_a, tenant_b, failed, monkeypatch):
    from apps.webhooks import tasks

    with tenant_context(tenant_a.org):
        _age(failed, 120)
    call_command("purge_webhook_deliveries", "--organization", tenant_b.org.slug)
    with tenant_context(tenant_a.org):
        assert WebhookDelivery.objects.exists()  # solo purgó B
    deferred = []
    monkeypatch.setattr(
        tasks.purge_organization_deliveries,
        "configure",
        lambda **kw: type("T", (), {"defer": staticmethod(lambda **k: deferred.append(k))}),
    )
    assert tasks.enqueue_delivery_purge(timestamp=0) >= 2
    assert {"organization_id": str(tenant_a.org.pk)} in deferred
    assert tasks.purge_organization_deliveries(organization_id=str(tenant_a.org.pk)) == 1
    with tenant_context(tenant_a.org):
        assert not WebhookDelivery.objects.exists()
