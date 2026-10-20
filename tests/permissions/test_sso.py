"""Permisos del SSO (ORG-11): solo administradores y solo con el módulo `sso` (plan Empresa)."""

import pytest
from django.urls import reverse

from apps.accounts.models import Membership, User
from apps.accounts.permissions import PermissionDeniedError
from apps.core.tenancy import tenant_context
from apps.organizations.models import Plan
from apps.organizations.plans import BASE_MODULES
from apps.sso import selectors, services
from apps.sso.models import SsoDomain
from tests.conftest import PASSWORD, login
from tests.sso_utils import FakeIdP, configure, install_dns, install_idps


@pytest.fixture
def idp(monkeypatch):
    provider = FakeIdP("tenant-a")
    install_idps(monkeypatch, provider)
    install_dns(monkeypatch)
    return provider


@pytest.fixture
def domain(tenant_a, idp):
    configure(tenant_a, idp)
    with tenant_context(tenant_a.org):
        return SsoDomain.objects.get()


def _planner(tenant):
    user = User.objects.create_user("planner@empresa-a.cl", PASSWORD, full_name="Planner")
    return Membership.objects.create(user=user, organization=tenant.org, role=Membership.Role.PLANNER)


@pytest.mark.parametrize("who", ["tech", "viewer", "planner"])
def test_non_admins_cannot_use_the_screens(client, tenant_a, domain, who):
    membership = _planner(tenant_a) if who == "planner" else getattr(tenant_a, who)
    login(client, membership)
    assert client.get(reverse("sso_settings")).status_code == 403
    assert client.post(reverse("sso_settings"), {"client_id": "x"}).status_code == 403
    assert client.post(reverse("sso_test")).status_code == 403
    assert client.post(reverse("sso_domain_add"), {"domain": "x.cl"}).status_code == 403
    assert client.post(reverse("sso_domain_verify", args=[domain.pk])).status_code == 403
    assert client.post(reverse("sso_domain_remove", args=[domain.pk])).status_code == 403
    assert reverse("sso_settings") not in client.get(reverse("menu")).content.decode()


@pytest.mark.parametrize("who", ["tech", "planner"])
def test_non_admins_are_rejected_by_services_and_selectors(tenant_a, domain, idp, who):
    membership = _planner(tenant_a) if who == "planner" else tenant_a.tech
    with tenant_context(tenant_a.org):
        for call in (
            lambda: services.save_configuration(
                membership=membership,
                discovery_url=idp.issuer,
                client_id="x",
                client_secret="y",
                default_role="technician",
            ),
            lambda: services.test_connection(membership=membership),
            lambda: services.add_domain(membership=membership, domain="x.cl"),
            lambda: services.verify_domain(membership=membership, domain=domain),
            lambda: services.remove_domain(membership=membership, domain=domain),
            lambda: selectors.get_configuration(membership=membership),
            lambda: selectors.domain_list(membership=membership),
        ):
            with pytest.raises(PermissionDeniedError):
                call()


def test_admin_sees_the_screen_and_the_links(client, tenant_a, domain):
    login(client, tenant_a.admin)
    assert client.get(reverse("sso_settings")).status_code == 200
    assert reverse("sso_settings") in client.get(reverse("menu")).content.decode()
    assert reverse("sso_settings") in client.get(reverse("organization_settings")).content.decode()


def test_plan_without_sso_hides_the_screen(client, tenant_a):
    tenant_a.org.plan = Plan.objects.create(
        code="pro-test", name="Profesional", features=[*BASE_MODULES, "api"]
    )
    tenant_a.org.save(update_fields=["plan"])
    login(client, tenant_a.admin)
    response = client.get(reverse("sso_settings"))
    assert response.status_code == 403 and "no está incluido" in response.content.decode()
    assert reverse("sso_settings") not in client.get(reverse("menu")).content.decode()
    assert reverse("sso_settings") not in client.get(reverse("organization_settings")).content.decode()


def test_only_the_enterprise_plan_includes_sso(db):
    from apps.organizations.plans import ensure_default_plans

    ensure_default_plans(Plan)
    with_sso = sorted(plan.code for plan in Plan.objects.all() if "sso" in plan.features)
    assert with_sso == ["enterprise"]
