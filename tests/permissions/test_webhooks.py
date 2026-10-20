"""Permisos de webhooks (X-08): solo quien administra integraciones y con el módulo `api`."""

import pytest
from django.urls import reverse

from apps.accounts.models import Membership, User
from apps.accounts.permissions import PermissionDeniedError
from apps.core.tenancy import tenant_context
from apps.organizations.models import Plan
from apps.webhooks import selectors, services
from apps.webhooks.models import Event
from tests.conftest import PASSWORD, login


@pytest.fixture
def network(monkeypatch):
    monkeypatch.setattr(services, "resolve_host", lambda host, port: ["93.184.216.34"])
    monkeypatch.setattr(services, "http_post", lambda url, *, address, body, headers: (200, ""))


@pytest.fixture
def endpoint(tenant_a, network):
    with tenant_context(tenant_a.org):
        return services.create_endpoint(
            membership=tenant_a.admin,
            name="ERP",
            url="https://erp.example.com/",
            events=[Event.WORK_ORDER_CREATED],
        )


def _planner(tenant):
    user = User.objects.create_user("planner@a.cl", PASSWORD, full_name="Planner")
    return Membership.objects.create(user=user, organization=tenant.org, role=Membership.Role.PLANNER)


@pytest.mark.parametrize("who", ["tech", "viewer", "planner"])
def test_non_admins_cannot_use_the_screens(client, tenant_a, endpoint, who):
    membership = _planner(tenant_a) if who == "planner" else getattr(tenant_a, who)
    login(client, membership)
    assert client.get(reverse("webhook_list")).status_code == 403
    assert client.get(reverse("webhook_detail", args=[endpoint.pk])).status_code == 403
    assert client.post(reverse("webhook_test", args=[endpoint.pk])).status_code == 403
    page = client.get(reverse("menu")).content.decode()
    assert reverse("webhook_list") not in page


@pytest.mark.parametrize("who", ["tech", "planner"])
def test_non_admins_are_rejected_by_the_services(tenant_a, endpoint, who):
    membership = _planner(tenant_a) if who == "planner" else tenant_a.tech
    with tenant_context(tenant_a.org):
        with pytest.raises(PermissionDeniedError):
            services.create_endpoint(
                membership=membership,
                name="x",
                url="https://x.example.com/",
                events=[Event.WORK_ORDER_CREATED],
            )
        with pytest.raises(PermissionDeniedError):
            services.send_test(membership=membership, endpoint=endpoint)
        with pytest.raises(PermissionDeniedError):
            selectors.endpoint_list(membership=membership)


def test_admin_sees_the_link_in_more(client, tenant_a):
    login(client, tenant_a.admin)
    assert reverse("webhook_list") in client.get(reverse("menu")).content.decode()


def test_plan_without_api_blocks_the_screens(client, tenant_a, endpoint):
    tenant_a.org.plan = Plan.objects.create(code="mini", name="Mini", features=["work_orders", "assets"])
    tenant_a.org.save()
    login(client, tenant_a.admin)
    assert client.get(reverse("webhook_list")).status_code == 403
    assert client.get(reverse("webhook_detail", args=[endpoint.pk])).status_code == 403
    assert reverse("webhook_list") not in client.get(reverse("menu")).content.decode()


@pytest.mark.parametrize("who", ["tech", "viewer", "planner"])
def test_non_admins_cannot_rotate_redeliver_or_see_bodies(client, tenant_a, endpoint, who):
    from apps.webhooks.models import WebhookDelivery

    with tenant_context(tenant_a.org):
        payload = services.build_payload(event_type=Event.WORK_ORDER_CREATED, data={})
        delivery = WebhookDelivery.objects.create(
            endpoint=endpoint,
            event_id=payload["id"],
            event_type=payload["type"],
            payload=payload,
            status=WebhookDelivery.Status.FAILED,
        )
    membership = _planner(tenant_a) if who == "planner" else getattr(tenant_a, who)
    login(client, membership)
    assert client.post(reverse("webhook_secret", args=[endpoint.pk])).status_code == 403
    assert client.get(reverse("webhook_delivery", args=[endpoint.pk, delivery.pk])).status_code == 403
    assert client.post(reverse("webhook_redeliver", args=[endpoint.pk, delivery.pk])).status_code == 403
    with tenant_context(tenant_a.org):
        with pytest.raises(PermissionDeniedError):
            services.regenerate_secret(membership=membership, endpoint=endpoint)
        with pytest.raises(PermissionDeniedError):
            services.redeliver(membership=membership, delivery=delivery)
        assert WebhookDelivery.objects.count() == 1


def test_read_only_billing_hides_write_actions(client, tenant_a, endpoint, monkeypatch):
    """Solo lectura por impago: la pantalla se ve, sin formularios de escritura (y el POST da 402)."""
    from apps.billing import access

    monkeypatch.setattr(
        access, "billing_status", lambda organization, **kw: access.BillingStatus(access.Access.READ_ONLY)
    )
    login(client, tenant_a.admin)
    detail = client.get(reverse("webhook_detail", args=[endpoint.pk])).content.decode()
    assert reverse("webhook_secret", args=[endpoint.pk]) not in detail
    assert reverse("webhook_test", args=[endpoint.pk]) not in detail
    assert 'name="url"' not in client.get(reverse("webhook_list")).content.decode()
    assert 'name="scopes"' not in client.get(reverse("api_token_list")).content.decode()
    assert client.post(reverse("webhook_secret", args=[endpoint.pk])).status_code == 402
