"""Permisos de la facturación (ORG-13): solo administradores (organization.manage)."""

import pytest
from django.urls import reverse

from apps.accounts.permissions import PermissionDeniedError
from apps.billing import services
from apps.billing.selectors import billing_overview, payment_history
from apps.core.tenancy import tenant_context
from apps.organizations.models import Plan
from tests.conftest import login

URLS = ["billing", "billing_checkout", "billing_change", "billing_cancel"]


@pytest.mark.parametrize("role", ["tech", "viewer"])
@pytest.mark.parametrize("url_name", URLS)
def test_only_admins_reach_billing(client, tenant_a, role, url_name):
    login(client, getattr(tenant_a, role))
    method = client.get if url_name == "billing" else client.post
    assert method(reverse(url_name)).status_code == 403


def test_admin_reaches_billing(client, tenant_a):
    login(client, tenant_a.admin)
    assert client.get(reverse("billing")).status_code == 200


@pytest.mark.parametrize("role", ["tech", "viewer"])
def test_services_and_selectors_require_admin(tenant_a, role):
    membership = getattr(tenant_a, role)
    basic = Plan.objects.get(code="basic")
    with tenant_context(tenant_a.org):
        for call in (
            lambda: billing_overview(membership=membership),
            lambda: payment_history(membership=membership),
            lambda: services.start_checkout(membership=membership, plan=basic, period="monthly"),
            lambda: services.change_plan(membership=membership, plan=basic),
            lambda: services.cancel_subscription(membership=membership),
            lambda: services.update_billing_details(
                membership=membership,
                tax_id="",
                legal_name="",
                business_activity="",
                billing_address="",
                billing_email="",
            ),
        ):
            with pytest.raises(PermissionDeniedError):
                call()


def test_menu_and_settings_link_only_for_admins(client, tenant_a):
    login(client, tenant_a.admin)
    assert reverse("billing") in client.get(reverse("menu")).content.decode()
    assert reverse("billing") in client.get(reverse("organization_settings")).content.decode()
    login(client, tenant_a.tech)
    assert reverse("billing") not in client.get(reverse("menu")).content.decode()
