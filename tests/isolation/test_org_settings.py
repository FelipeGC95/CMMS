"""La configuración de la organización solo lee y escribe la organización activa."""

from django.urls import reverse

from apps.accounts.models import Membership
from tests.conftest import login


def test_settings_page_shows_and_changes_only_the_active_organization(client, tenant_a, tenant_b):
    login(client, tenant_a.admin)
    body = client.get(reverse("organization_settings")).content.decode()
    assert tenant_a.org.name in body
    assert tenant_b.org.name not in body

    client.post(reverse("organization_settings"), {"require_close_approval": "on"})
    tenant_a.org.refresh_from_db()
    tenant_b.org.refresh_from_db()
    assert tenant_a.org.require_close_approval is True
    assert tenant_b.org.require_close_approval is False


def test_admin_elsewhere_cannot_change_an_organization_where_they_are_not_admin(client, tenant_a, tenant_b):
    """Ser administrador en B no da permisos sobre A: cuenta el rol en la organización activa."""
    Membership.objects.create(user=tenant_b.admin.user, organization=tenant_a.org, role="viewer")
    login(client, tenant_b.admin)
    client.post(reverse("switch_organization"), {"organization": str(tenant_a.org.pk)})
    assert client.post(reverse("organization_settings"), {"require_close_approval": "on"}).status_code == 403
    tenant_a.org.refresh_from_db()
    tenant_b.org.refresh_from_db()
    assert tenant_a.org.require_close_approval is False
    assert tenant_b.org.require_close_approval is False
