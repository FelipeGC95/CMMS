"""ORG-08: la política de MFA de una organización no toca a otra."""

from django.urls import reverse

from apps.organizations.models import Organization
from tests.conftest import login


def test_policy_change_only_affects_own_organization(client, tenant_a, tenant_b):
    login(client, tenant_a.admin)
    client.post(reverse("organization_settings"), {"section": "security", "mfa_policy": "everyone"})
    tenant_a.org.refresh_from_db()
    tenant_b.org.refresh_from_db()
    assert tenant_a.org.mfa_policy == Organization.MfaPolicy.EVERYONE
    assert tenant_b.org.mfa_policy == Organization.MfaPolicy.ADMINS_ONLY
