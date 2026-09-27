"""ORG-03: solo los administradores de la organización usan el asistente."""

import pytest
from django.urls import reverse

from apps.accounts.permissions import PermissionDeniedError
from apps.core.tenancy import tenant_context
from apps.onboarding import services
from apps.onboarding.models import STEPS
from apps.onboarding.selectors import onboarding_for_dashboard
from tests.conftest import login

GET_URLS = [reverse("onboarding")] + [reverse("onboarding_step", args=[s]) for s in STEPS]
POST_URLS = [reverse("onboarding_dismiss")] + [reverse("onboarding_skip", args=[s]) for s in STEPS]


@pytest.mark.parametrize("role", ["tech", "viewer"])
def test_non_admins_get_403(client, tenant_a, role):
    login(client, getattr(tenant_a, role))
    for url in GET_URLS:
        assert client.get(url).status_code == 403, url
    for url in POST_URLS + GET_URLS[1:]:
        assert client.post(url).status_code == 403, url


def test_admin_opens_every_step(client, tenant_a):
    login(client, tenant_a.admin)
    for url in GET_URLS[1:]:
        assert client.get(url).status_code == 200, url


def test_services_require_admin(tenant_a):
    with tenant_context(tenant_a.org):
        with pytest.raises(PermissionDeniedError):
            services.skip_step(membership=tenant_a.tech, step="industry")
        with pytest.raises(PermissionDeniedError):
            services.choose_industry(membership=tenant_a.viewer, industry="fleet")
        services.open_wizard(membership=tenant_a.admin)
        # Técnicos y lectores no ven el aviso del inicio.
        assert onboarding_for_dashboard(membership=tenant_a.tech) is None
        assert onboarding_for_dashboard(membership=tenant_a.admin) is not None


def test_new_org_technician_is_not_redirected(client, tenant_a):
    with tenant_context(tenant_a.org):
        services.create_for_new_organization(organization=tenant_a.org)
    login(client, tenant_a.tech)
    assert client.get(reverse("dashboard")).status_code == 200
    login(client, tenant_a.admin)
    assert client.get(reverse("dashboard"))["Location"] == reverse("onboarding")
