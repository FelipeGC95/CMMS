"""Aislamiento del SSO (ORG-11): A no ve ni usa la configuración ni los dominios de B,
y el IdP de A no puede abrir sesión en B."""

from urllib.parse import parse_qs, urlsplit

import pytest
from django.urls import reverse

from apps.accounts.models import Membership, User
from apps.core.tenancy import tenant_context
from apps.sso import selectors, services
from apps.sso.models import SsoConfiguration, SsoDomain
from tests.conftest import login
from tests.sso_utils import FakeIdP, configure, install_dns, install_idps, sso_round_trip


@pytest.fixture
def idps(monkeypatch):
    idp_a, idp_b = FakeIdP("tenant-a"), FakeIdP("tenant-b", client_id="app-b", key_name="b")
    install_idps(monkeypatch, idp_a, idp_b)
    install_dns(monkeypatch)
    return idp_a, idp_b


@pytest.fixture
def both(tenant_a, tenant_b, idps):
    idp_a, idp_b = idps
    configure(tenant_a, idp_a, domains=("empresa-a.cl",))
    configure(tenant_b, idp_b, domains=("empresa-b.cl",))
    return idp_a, idp_b


def test_managers_only_see_their_organization(tenant_a, tenant_b, both):
    with tenant_context(tenant_a.org):
        assert [d.domain for d in SsoDomain.objects.all()] == ["empresa-a.cl"]
        assert SsoConfiguration.objects.get().client_id == "galbust-app"
        assert [d.domain for d in selectors.domain_list(membership=tenant_a.admin)] == ["empresa-a.cl"]
        assert selectors.get_configuration(membership=tenant_a.admin).client_id == "galbust-app"


def test_admin_of_a_cannot_verify_or_remove_domains_of_b(client, tenant_a, tenant_b, both):
    with tenant_context(tenant_b.org):
        pending_b = services.add_domain(membership=tenant_b.admin, domain="pendiente-b.cl")
        verified_b = SsoDomain.objects.get(domain="empresa-b.cl")
    login(client, tenant_a.admin)
    assert client.post(reverse("sso_domain_verify", args=[pending_b.pk])).status_code == 404
    assert client.post(reverse("sso_domain_remove", args=[verified_b.pk])).status_code == 404
    page = client.get(reverse("sso_settings")).content.decode()
    assert "empresa-b.cl" not in page and "pendiente-b.cl" not in page and "app-b" not in page
    with tenant_context(tenant_b.org):
        assert SsoDomain.objects.count() == 2


def test_saving_in_a_does_not_touch_b(client, tenant_a, tenant_b, both):
    idp_a, _ = both
    login(client, tenant_a.admin)
    client.post(
        reverse("sso_settings"),
        {"discovery_url": idp_a.issuer, "client_id": "cambiado", "default_role": "viewer", "is_active": "on"},
    )
    with tenant_context(tenant_b.org):
        assert SsoConfiguration.objects.get().client_id == "app-b"


def test_email_domain_routes_to_its_own_organization(tenant_a, tenant_b, both):
    assert selectors.sso_for_email("x@empresa-a.cl").organization == tenant_a.org
    assert selectors.sso_for_email("x@empresa-b.cl").organization == tenant_b.org


def test_idp_of_a_cannot_sign_in_users_of_b(client, tenant_a, tenant_b, both):
    """Un IdP de A que afirma un correo del dominio de B no abre sesión ni crea nada en B."""
    idp_a, _ = both
    start = client.post(reverse("sso_login"), {"email": "alguien@empresa-a.cl"})
    query = {key: values[0] for key, values in parse_qs(urlsplit(start["Location"]).query).items()}
    idp_a.nonce = query["nonce"]
    response = client.get(
        reverse("sso_callback"), {"code": "code-for:admin@empresa-b.cl", "state": query["state"]}
    )
    assert response.status_code == 400
    assert "_auth_user_id" not in client.session
    assert not Membership.objects.filter(user=tenant_b.admin.user, organization=tenant_a.org).exists()


def test_token_signed_by_idp_of_b_is_rejected_for_a(client, tenant_a, tenant_b, both):
    idp_a, idp_b = both
    idp_a.signing_key = idp_b.key  # la firma es de B, el emisor dice A
    response = sso_round_trip(client, idp_a, "nueva@empresa-a.cl")
    assert response.status_code == 400
    assert not User.objects.filter(email="nueva@empresa-a.cl").exists()


def test_sso_login_opens_the_domain_organization_only(client, tenant_a, tenant_b, both):
    idp_a, _ = both
    Membership.objects.create(user=tenant_a.tech.user, organization=tenant_b.org, role="viewer")
    sso_round_trip(client, idp_a, "technician@empresa-a.cl")
    assert client.session["active_organization_id"] == str(tenant_a.org.pk)
    assert client.session["sso_organization_id"] == str(tenant_a.org.pk)
    # El alta automática solo crea membresía en A.
    sso_round_trip(client.__class__(), idp_a, "nueva@empresa-a.cl")
    assert list(
        Membership.objects.filter(user__email="nueva@empresa-a.cl").values_list("organization", flat=True)
    ) == [tenant_a.org.pk]
