"""Vista de terreno (ADR-011): lo que se guarda en el teléfono no lleva datos de ninguna organización."""

from django.urls import reverse

from tests.conftest import login


def test_shell_does_not_reveal_any_organization(client, tenant_a, tenant_b):
    login(client, tenant_a.admin)
    body = client.get(reverse("field_shell")).content.decode()
    for tenant in (tenant_a, tenant_b):
        assert tenant.org.name not in body
        assert str(tenant.org.pk) not in body
        assert tenant.asset.name not in body


def test_service_worker_and_manifest_are_the_same_for_every_organization(client, tenant_a, tenant_b):
    anonymous = [client.get(reverse(name)).content for name in ("field_sw", "field_manifest")]
    for membership in (tenant_a.tech, tenant_b.tech):
        login(client, membership)
        assert [client.get(reverse(name)).content for name in ("field_sw", "field_manifest")] == anonymous
        client.logout()
