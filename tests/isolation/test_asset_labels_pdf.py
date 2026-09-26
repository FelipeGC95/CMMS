"""Etiquetas QR en PDF: B no imprime activos de A aunque pase sus ids, ni ve su logo."""

import pytest
from django.urls import reverse

from apps.core.tenancy import tenant_context
from apps.organizations.services import set_organization_logo
from tests.conftest import login
from tests.test_asset_labels_pdf import pdf_text
from tests.test_org_logo import image_upload


@pytest.fixture(autouse=True)
def _media(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path


def test_ids_of_another_organization_print_nothing(client, tenant_a, tenant_b):
    login(client, tenant_b.admin)
    response = client.get(reverse("asset_labels_pdf"), {"scope": "selected", "ids": [str(tenant_a.asset.pk)]})
    assert response.status_code == 302
    assert response["Location"].startswith(reverse("asset_list"))


def test_mixed_ids_print_only_the_own_assets(client, tenant_a, tenant_b):
    with tenant_context(tenant_a.org):
        tenant_a.asset.code = "SOLO-A"
        tenant_a.asset.save(update_fields=["code"])
    login(client, tenant_b.admin)
    response = client.get(
        reverse("asset_labels_pdf"),
        {"scope": "selected", "ids": [str(tenant_a.asset.pk), str(tenant_b.asset.pk)]},
    )
    text = pdf_text(response.content)
    assert "(CMP-01) Tj" in text
    assert "SOLO-A" not in text
    assert tenant_a.asset.qr_token not in text
    assert tenant_a.org.name not in text


def test_parent_of_another_organization_is_404(client, tenant_a, tenant_b):
    login(client, tenant_b.admin)
    assert client.get(reverse("asset_labels_pdf"), {"parent": str(tenant_a.asset.pk)}).status_code == 404


def test_logo_of_another_organization_is_not_used(client, tenant_a, tenant_b):
    with tenant_context(tenant_a.org):
        set_organization_logo(membership=tenant_a.admin, uploaded_file=image_upload())
    login(client, tenant_b.admin)
    response = client.get(reverse("asset_labels_pdf"))
    assert b"/Subtype /Image" not in response.content
    assert f"({tenant_b.org.name}) Tj" in pdf_text(response.content)
