"""El aviso "Faltan N datos técnicos obligatorios" y su enlace "Completar" (AST-01) son solo
para quienes gestionan activos; el formulario de datos técnicos sigue exigiendo `asset.manage`."""

import pytest
from django.urls import reverse

from apps.assets.models import AssetClassAttribute
from apps.core.tenancy import tenant_context
from tests.conftest import login


@pytest.fixture
def incomplete_a(tenant_a):
    with tenant_context(tenant_a.org):
        AssetClassAttribute.objects.create(
            asset_class=tenant_a.asset.asset_class,
            key="potencia",
            label="Potencia",
            data_type="text",
            required=True,
        )
    return tenant_a.asset


@pytest.mark.parametrize("role", ["tech", "viewer"])
def test_notice_and_link_hidden_without_asset_manage(client, tenant_a, incomplete_a, role):
    login(client, getattr(tenant_a, role))
    url = reverse("asset_attributes", args=[incomplete_a.pk])
    html = client.get(reverse("asset_detail", args=[incomplete_a.pk])).content.decode()
    assert "Falta 1 dato técnico obligatorio" not in html and url not in html
    assert client.get(url).status_code == 403


def test_notice_and_link_for_managers(client, tenant_a, incomplete_a):
    login(client, tenant_a.admin)
    html = client.get(reverse("asset_detail", args=[incomplete_a.pk])).content.decode()
    assert "Falta 1 dato técnico obligatorio" in html
    assert f'<a href="{reverse("asset_attributes", args=[incomplete_a.pk])}">' in html
