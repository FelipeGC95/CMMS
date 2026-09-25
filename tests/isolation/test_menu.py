"""El menú "Más" solo muestra la organización propia (ADR-017)."""

from django.urls import reverse

from tests.conftest import login


def test_menu_does_not_reveal_other_organizations(client, tenant_a, tenant_b):
    login(client, tenant_a.admin)
    body = client.get(reverse("menu")).content.decode()
    assert tenant_a.org.name in body
    assert tenant_b.org.name not in body


def test_menu_requires_login(client, tenant_a):
    response = client.get(reverse("menu"))
    assert response.status_code == 302
    assert reverse("account_login") in response["Location"]
