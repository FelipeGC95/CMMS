"""ORG-08: solo el administrador cambia la política de MFA de la organización."""

import pytest
from django.urls import reverse

from apps.organizations.models import Organization
from tests.conftest import login


@pytest.mark.parametrize("role", ["tech", "viewer"])
def test_non_admin_cannot_change_policy(client, tenant_a, role):
    login(client, getattr(tenant_a, role))
    response = client.post(
        reverse("organization_settings"), {"section": "security", "mfa_policy": "optional"}
    )
    assert response.status_code == 403
    tenant_a.org.refresh_from_db()
    assert tenant_a.org.mfa_policy == Organization.MfaPolicy.ADMINS_ONLY
