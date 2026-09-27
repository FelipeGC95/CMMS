"""ORG-09: solo el administrador cambia el color de la organización."""

import pytest
from django.urls import reverse

from tests.conftest import login


@pytest.mark.parametrize("role", ["tech", "viewer"])
def test_non_admin_cannot_change_color(client, tenant_a, role):
    login(client, getattr(tenant_a, role))
    response = client.post(reverse("organization_settings"), {"section": "brand", "primary_color": "#7C3AED"})
    assert response.status_code == 403
    tenant_a.org.refresh_from_db()
    assert tenant_a.org.primary_color == ""


def test_anonymous_theme_css_is_empty(client, db):
    response = client.get(reverse("organization_theme_css"))
    assert response.status_code == 200 and "--color" not in response.content.decode()
