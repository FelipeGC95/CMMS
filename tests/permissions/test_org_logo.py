"""Solo el administrador sube o quita el logo; cualquier miembro lo ve (va en sus etiquetas)."""

import pytest
from django.urls import reverse

from apps.accounts.permissions import PermissionDeniedError
from apps.core.tenancy import tenant_context
from apps.organizations.services import remove_organization_logo, set_organization_logo
from tests.conftest import login
from tests.test_org_logo import image_upload

ROLES = ["planner", "supervisor", "technician", "viewer", "external"]


@pytest.fixture(autouse=True)
def _media(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path


@pytest.mark.parametrize("role", ROLES)
def test_service_rejects_non_admins(tenant_a, role):
    tenant_a.tech.role = role
    tenant_a.tech.save(update_fields=["role"])
    with tenant_context(tenant_a.org):
        with pytest.raises(PermissionDeniedError):
            set_organization_logo(membership=tenant_a.tech, uploaded_file=image_upload())
        with pytest.raises(PermissionDeniedError):
            remove_organization_logo(membership=tenant_a.tech)
    tenant_a.org.refresh_from_db()
    assert not tenant_a.org.logo


@pytest.mark.parametrize("role", ROLES)
def test_view_rejects_non_admins(client, tenant_a, role):
    tenant_a.tech.role = role
    tenant_a.tech.save(update_fields=["role"])
    login(client, tenant_a.tech)
    response = client.post(reverse("organization_settings"), {"section": "logo", "logo": image_upload()})
    assert response.status_code == 403
    tenant_a.org.refresh_from_db()
    assert not tenant_a.org.logo


@pytest.mark.parametrize("role", ROLES)
def test_any_member_can_fetch_the_logo(client, tenant_a, role):
    with tenant_context(tenant_a.org):
        set_organization_logo(membership=tenant_a.admin, uploaded_file=image_upload())
    tenant_a.tech.role = role
    tenant_a.tech.save(update_fields=["role"])
    login(client, tenant_a.tech)
    assert client.get(reverse("organization_logo")).status_code == 200


def test_anonymous_cannot_fetch_the_logo(client, tenant_a):
    assert client.get(reverse("organization_logo")).status_code == 302
