"""Aislamiento de webhooks (X-08): A no ve, edita, prueba ni recibe eventos de B."""

import pytest
from django.urls import reverse

from apps.core.tenancy import tenant_context
from apps.webhooks import services
from apps.webhooks.models import Event, WebhookDelivery, WebhookEndpoint
from apps.work.services import create_work_order
from tests.conftest import login


@pytest.fixture
def network(monkeypatch):
    calls = []
    monkeypatch.setattr(services, "resolve_host", lambda host, port: ["93.184.216.34"])
    monkeypatch.setattr(
        services, "http_post", lambda url, *, address, body, headers: calls.append(url) or 200
    )
    return calls


def _endpoint(tenant, url):
    with tenant_context(tenant.org):
        return services.create_endpoint(
            membership=tenant.admin, name=tenant.org.slug, url=url, events=[Event.WORK_ORDER_CREATED]
        )


def test_events_only_reach_endpoints_of_the_same_organization(
    tenant_a, tenant_b, network, django_capture_on_commit_callbacks
):
    _endpoint(tenant_a, "https://a.example.com/")
    _endpoint(tenant_b, "https://b.example.com/")
    with tenant_context(tenant_a.org), django_capture_on_commit_callbacks(execute=True):
        create_work_order(work_type="corrective", title="x", asset=tenant_a.asset)
    assert network == ["https://a.example.com/"]
    with tenant_context(tenant_b.org):
        assert not WebhookDelivery.objects.exists()
    with tenant_context(tenant_a.org):
        assert WebhookDelivery.objects.get().organization_id == tenant_a.org.pk


def test_managers_only_see_their_organization(tenant_a, tenant_b, network):
    _endpoint(tenant_b, "https://b.example.com/")
    with tenant_context(tenant_a.org):
        assert not WebhookEndpoint.objects.exists()


def test_admin_of_a_cannot_open_edit_or_test_endpoint_of_b(client, tenant_a, tenant_b, network):
    endpoint_b = _endpoint(tenant_b, "https://b.example.com/")
    login(client, tenant_a.admin)
    assert client.get(reverse("webhook_detail", args=[endpoint_b.pk])).status_code == 404
    response = client.post(
        reverse("webhook_detail", args=[endpoint_b.pk]),
        {"name": "robado", "url": "https://evil.example.com/", "events": [Event.WORK_ORDER_CREATED]},
    )
    assert response.status_code == 404
    assert client.post(reverse("webhook_test", args=[endpoint_b.pk])).status_code == 404
    list_page = client.get(reverse("webhook_list")).content.decode()
    assert "b.example.com" not in list_page and endpoint_b.secret not in list_page
    with tenant_context(tenant_b.org):
        endpoint_b.refresh_from_db()
        assert endpoint_b.name == tenant_b.org.slug
        assert not WebhookDelivery.objects.exists()
