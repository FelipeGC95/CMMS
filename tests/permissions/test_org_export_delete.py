"""Solo el administrador exporta todos los datos o elimina la organización (X-03, X-04)."""

import pytest
from django.urls import reverse

from apps.accounts.permissions import PermissionDeniedError
from apps.core.tenancy import tenant_context
from apps.organizations import services
from apps.organizations.models import DataExport, Organization
from tests.conftest import PASSWORD, login

ROLES = ["planner", "supervisor", "technician", "viewer", "external"]


@pytest.fixture(autouse=True)
def _media(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    settings.DATA_EXPORT_USE_QUEUE = False


@pytest.mark.parametrize("role", ROLES)
def test_services_reject_non_admins(tenant_a, role):
    tenant_a.tech.role = role
    tenant_a.tech.save(update_fields=["role"])
    with tenant_context(tenant_a.org):
        with pytest.raises(PermissionDeniedError):
            services.request_data_export(membership=tenant_a.tech)
        with pytest.raises(PermissionDeniedError):
            services.request_organization_deletion(
                membership=tenant_a.tech, password=PASSWORD, confirm_name=tenant_a.org.name
            )
        with pytest.raises(PermissionDeniedError):
            services.cancel_organization_deletion(membership=tenant_a.tech)
        assert not DataExport.objects.exists()


@pytest.mark.parametrize("role", ROLES)
def test_views_reject_non_admins(client, tenant_a, role, django_capture_on_commit_callbacks):
    with tenant_context(tenant_a.org):
        with django_capture_on_commit_callbacks(execute=True):
            export = services.request_data_export(membership=tenant_a.admin)
    tenant_a.tech.role = role
    tenant_a.tech.save(update_fields=["role"])
    login(client, tenant_a.tech)
    assert client.post(reverse("data_export_request")).status_code == 403
    assert client.get(reverse("data_export_download", args=[export.pk])).status_code == 403
    assert client.get(reverse("organization_delete")).status_code == 403
    response = client.post(
        reverse("organization_delete"), {"password": PASSWORD, "confirm_name": tenant_a.org.name}
    )
    assert response.status_code == 403
    assert client.post(reverse("organization_delete_cancel")).status_code == 403
    tenant_a.org.refresh_from_db()
    assert tenant_a.org.status == Organization.Status.ACTIVE


def test_admin_can_open_delete_page(client, tenant_a):
    login(client, tenant_a.admin)
    response = client.get(reverse("organization_delete"))
    assert response.status_code == 200
    assert reverse("data_export_request") in response.content.decode()
