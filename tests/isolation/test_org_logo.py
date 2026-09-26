"""El logo es de la organización activa: B nunca ve ni cambia el de A."""

import pytest
from django.urls import reverse

from apps.core.tenancy import tenant_context
from apps.organizations.services import set_organization_logo
from tests.conftest import login
from tests.test_org_logo import image_upload


@pytest.fixture(autouse=True)
def _media(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path


def test_other_organization_cannot_see_the_logo(client, tenant_a, tenant_b):
    with tenant_context(tenant_a.org):
        set_organization_logo(membership=tenant_a.admin, uploaded_file=image_upload())
    login(client, tenant_b.admin)
    assert client.get(reverse("organization_logo")).status_code == 404
    assert reverse("organization_logo") not in client.get(reverse("organization_settings")).content.decode()


def test_upload_changes_only_the_active_organization(client, tenant_a, tenant_b):
    login(client, tenant_b.admin)
    client.post(reverse("organization_settings"), {"section": "logo", "logo": image_upload()})
    tenant_a.org.refresh_from_db()
    tenant_b.org.refresh_from_db()
    assert not tenant_a.org.logo
    assert tenant_b.org.logo.name.startswith(f"org/{tenant_b.org.pk}/")
