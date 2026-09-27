"""ORG-09: la hoja de colores es la de la organización activa; nunca la de otra."""

from django.urls import reverse

from apps.core.tenancy import tenant_context
from apps.organizations.services import set_primary_color
from tests.conftest import login


def test_theme_css_belongs_to_active_organization(client, tenant_a, tenant_b):
    with tenant_context(tenant_b.org):
        set_primary_color(membership=tenant_b.admin, color="#7C3AED")
    login(client, tenant_a.admin)
    css = client.get(reverse("organization_theme_css")).content.decode()
    assert "--color-action-bg" not in css
    tenant_a.org.refresh_from_db()
    assert tenant_a.org.primary_color == ""
