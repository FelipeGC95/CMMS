"""Vista de terreno (ADR-011): lo que se guarda en el teléfono no lleva datos de ninguna organización."""

from django.urls import reverse

from apps.accounts.models import Membership
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


def test_shell_does_not_reveal_the_person(client, tenant_a):
    login(client, tenant_a.tech)
    body = client.get(reverse("field_shell")).content.decode()
    user = tenant_a.tech.user
    for value in (user.email, user.full_name, str(user.pk)):
        assert value not in body
    # Sin token CSRF en el HTML guardado: "Salir" lo toma de la cookie al enviar.
    assert 'name="csrfmiddlewaretoken" value=""' in body


def test_web_pages_carry_the_active_person_and_organization_only(client, tenant_a, tenant_b):
    """static/js/app.js compara estos datos con los de IndexedDB y borra los de otra organización."""
    Membership.objects.create(user=tenant_a.tech.user, organization=tenant_b.org, role="technician")
    login(client, tenant_a.tech)
    html = client.get(reverse("dashboard")).content.decode()
    assert f'data-user-id="{tenant_a.tech.user.pk}" data-organization-id="{tenant_a.org.pk}"' in html
    assert f'data-organization-id="{tenant_b.org.pk}"' not in html
    client.post(reverse("switch_organization"), {"organization": str(tenant_b.org.pk)})
    html = client.get(reverse("dashboard")).content.decode()
    assert f'data-user-id="{tenant_a.tech.user.pk}" data-organization-id="{tenant_b.org.pk}"' in html
    # El selector de organización también lo intercepta app.js.
    assert 'class="org-switch" data-field-switch' in html
