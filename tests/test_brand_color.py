"""ORG-09: el administrador elige el color primario; se inyecta como hoja de estilos propia."""

import pytest
from django.core.exceptions import ValidationError
from django.urls import reverse

from apps.accounts.permissions import PermissionDeniedError
from apps.core.tenancy import tenant_context
from apps.organizations.services import set_primary_color
from tests.conftest import login


def test_admin_sets_color_and_is_told_about_adjustments(client, tenant_a):
    login(client, tenant_a.admin)
    response = client.post(
        reverse("organization_settings"), {"section": "brand", "primary_color": "#ffff00"}, follow=True
    )
    tenant_a.org.refresh_from_db()
    assert tenant_a.org.primary_color == "#FFFF00"
    assert "un tono más oscuro" in response.content.decode()


def test_theme_css_is_linked_and_served_with_etag(client, tenant_a):
    with tenant_context(tenant_a.org):
        set_primary_color(membership=tenant_a.admin, color="#7C3AED")
    login(client, tenant_a.admin)
    page = client.get(reverse("dashboard")).content.decode()
    assert f"{reverse('organization_theme_css')}?v=7c3aed" in page
    response = client.get(reverse("organization_theme_css"))
    assert response.status_code == 200 and response["Content-Type"].startswith("text/css")
    assert "--color-action-bg" in response.content.decode()
    assert response["Cache-Control"] == "private, max-age=86400"
    again = client.get(reverse("organization_theme_css"), HTTP_IF_NONE_MATCH=response["ETag"])
    assert again.status_code == 304


def test_reset_goes_back_to_galbust(client, tenant_a):
    with tenant_context(tenant_a.org):
        set_primary_color(membership=tenant_a.admin, color="#7C3AED")
    login(client, tenant_a.admin)
    client.post(
        reverse("organization_settings"), {"section": "brand", "primary_color": "#000000", "reset": "on"}
    )
    tenant_a.org.refresh_from_db()
    assert tenant_a.org.primary_color == ""
    assert reverse("organization_theme_css") not in client.get(reverse("dashboard")).content.decode()


def test_without_color_no_stylesheet_is_linked(client, tenant_a):
    login(client, tenant_a.admin)
    assert reverse("organization_theme_css") not in client.get(reverse("dashboard")).content.decode()


def test_only_admin_and_valid_hex(tenant_a):
    with tenant_context(tenant_a.org):
        with pytest.raises(PermissionDeniedError):
            set_primary_color(membership=tenant_a.tech, color="#7C3AED")
        with pytest.raises(ValidationError):
            set_primary_color(membership=tenant_a.admin, color="url(javascript:x)")
