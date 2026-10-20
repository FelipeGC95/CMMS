"""Aislamiento de la facturación (ORG-13 CA9): una organización no ve, edita ni paga por otra."""

import pytest
from django.urls import reverse

from apps.billing import services
from apps.billing.models import BillingAccount, Payment, Period
from apps.core.tenancy import CrossTenantError, tenant_context
from apps.organizations.models import Organization, Plan
from tests.billing_utils import authorize, install_gateway, make_billable, pay, store_uf
from tests.conftest import login


@pytest.fixture
def gateway(monkeypatch, settings):
    return install_gateway(monkeypatch, settings)


def _subscribe(tenant, gateway):
    make_billable(tenant.org)
    with tenant_context(tenant.org):
        store_uf()
        url = services.start_checkout(
            membership=tenant.admin, plan=Plan.objects.get(code="basic"), period=Period.MONTHLY
        )
    subscription_id = url.rsplit("/", 1)[1]
    authorize(gateway, subscription_id)
    return subscription_id


def test_accounts_and_payments_are_per_organization(tenant_a, tenant_b, gateway):
    subscription_a = _subscribe(tenant_a, gateway)
    pay(gateway, subscription_a, "pay-a")
    with tenant_context(tenant_b.org):
        assert not BillingAccount.objects.exists()
        assert not Payment.objects.exists()
    with tenant_context(tenant_a.org):
        account_a = BillingAccount.objects.get()
    with tenant_context(tenant_b.org), pytest.raises(CrossTenantError):
        Payment(account=account_a, provider_payment_id="x", status="approved", amount_clp=1).save()


def test_payment_of_one_subscription_only_touches_its_organization(tenant_a, tenant_b, gateway):
    subscription_a = _subscribe(tenant_a, gateway)
    _subscribe(tenant_b, gateway)
    before_b = Organization.objects.get(pk=tenant_b.org.pk).paid_through
    pay(gateway, subscription_a, "pay-a")
    assert Organization.objects.get(pk=tenant_a.org.pk).paid_through is not None
    assert Organization.objects.get(pk=tenant_b.org.pk).paid_through == before_b
    # El mismo id de pago informado luego para la suscripción de B no se registra en B.
    with tenant_context(tenant_b.org):
        subscription_b = BillingAccount.objects.get().provider_subscription_id
    pay(gateway, subscription_b, "pay-a", key="otra-clave")
    with tenant_context(tenant_b.org):
        assert not Payment.objects.exists()


def test_checkout_uses_the_own_external_reference(tenant_a, tenant_b, gateway):
    _subscribe(tenant_a, gateway)
    _subscribe(tenant_b, gateway)
    references = [call[1]["external_reference"] for call in gateway.calls if call[0] == "create"]
    with tenant_context(tenant_a.org):
        ref_a = BillingAccount.objects.get().external_reference
    with tenant_context(tenant_b.org):
        ref_b = BillingAccount.objects.get().external_reference
    assert references == [ref_a, ref_b] and ref_a != ref_b


def test_billing_page_shows_only_own_history(client, tenant_a, tenant_b, gateway):
    subscription_a = _subscribe(tenant_a, gateway)
    pay(gateway, subscription_a, "pay-de-a")
    login(client, tenant_b.admin)
    body = client.get(reverse("billing")).content.decode()
    assert "pay-de-a" not in body
    login(client, tenant_a.admin)
    assert "pay-de-a" in client.get(reverse("billing")).content.decode()


def test_billing_details_are_saved_in_the_active_organization(client, tenant_a, tenant_b):
    login(client, tenant_a.admin)
    client.post(reverse("billing"), {"tax_id": "76.123.456-0", "legal_name": "Empresa A SpA"})
    with tenant_context(tenant_b.org):
        assert not BillingAccount.objects.exists()
    with tenant_context(tenant_a.org):
        assert BillingAccount.objects.get().legal_name == "Empresa A SpA"
