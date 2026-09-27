"""Quién imprime etiquetas QR: cualquier miembro (como la página anterior), dentro de su alcance."""

import pytest
from django.urls import reverse

from apps.accounts.models import MembershipLocation
from apps.assets.models import Asset
from apps.core.tenancy import tenant_context
from apps.organizations.models import Location
from tests.conftest import login
from tests.test_asset_labels_pdf import pdf_text


@pytest.mark.parametrize("role", ["admin", "planner", "supervisor", "technician", "viewer"])
def test_every_role_can_print_labels(client, tenant_a, role):
    tenant_a.tech.role = role
    tenant_a.tech.save(update_fields=["role"])
    login(client, tenant_a.tech)
    response = client.get(reverse("asset_labels_pdf"))
    assert response.status_code == 200
    assert "CMP-01" in pdf_text(response.content)


def test_external_without_locations_has_nothing_to_print(client, tenant_a):
    """El externo solo ve activos de sus ubicaciones (ORG-06): sin ubicaciones no hay etiquetas."""
    tenant_a.tech.role = "external"
    tenant_a.tech.save(update_fields=["role"])
    login(client, tenant_a.tech)
    response = client.get(reverse("asset_labels_pdf"), follow=True)
    assert "No hay activos para imprimir" in response.content.decode()


def test_anonymous_is_sent_to_login(client, tenant_a):
    response = client.get(reverse("asset_labels_pdf"))
    assert response.status_code == 302
    assert "login" in response["Location"]


def test_site_scoped_member_prints_only_assets_in_scope(client, tenant_a):
    with tenant_context(tenant_a.org):
        north = Location.objects.create(code="N", name="Planta Norte")
        south = Location.objects.create(code="S", name="Planta Sur")
        cls = tenant_a.asset.asset_class
        inside = Asset.objects.create(code="IN-01", name="Llenadora", asset_class=cls, location=north)
        outside = Asset.objects.create(code="OUT-01", name="Caldera", asset_class=cls, location=south)
        MembershipLocation.objects.create(membership=tenant_a.tech, location=north)
    tenant_a.tech.restricted_to_locations = True
    tenant_a.tech.save()
    login(client, tenant_a.tech)

    text = pdf_text(client.get(reverse("asset_labels_pdf")).content)
    assert "IN-01" in text
    assert "OUT-01" not in text and "CMP-01" not in text

    response = client.get(
        reverse("asset_labels_pdf"), {"scope": "selected", "ids": [str(inside.pk), str(outside.pk)]}
    )
    text = pdf_text(response.content)
    assert "IN-01" in text and "OUT-01" not in text
