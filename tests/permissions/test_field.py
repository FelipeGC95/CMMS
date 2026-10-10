"""Vista de terreno (ADR-011): quién entra a /field/ y qué queda abierto sin sesión ni MFA."""

import pytest
from django.urls import reverse

from apps.accounts.models import User
from apps.core.tenancy import tenant_context
from apps.organizations.models import Organization, Plan
from apps.organizations.services import update_mfa_policy
from tests.conftest import PASSWORD, login


@pytest.mark.parametrize("role", ["admin", "tech", "viewer"])
def test_every_role_with_membership_opens_the_shell(client, tenant_a, role):
    login(client, getattr(tenant_a, role))
    assert client.get(reverse("field_shell")).status_code == 200


def test_user_without_organization_is_refused(client, db):
    client.force_login(User.objects.create_user("suelto@x.cl", PASSWORD))
    assert client.get(reverse("field_shell")).status_code == 403


@pytest.mark.parametrize("role", ["admin", "tech", "viewer"])
def test_plan_without_work_orders_blocks_the_shell(client, tenant_a, role):
    tenant_a.org.plan = Plan.objects.create(code="solo-activos", name="Solo activos", features=["assets"])
    tenant_a.org.save(update_fields=["plan"])
    login(client, getattr(tenant_a, role))
    assert client.get(reverse("field_shell")).status_code == 403
    assert client.get(reverse("field_demo")).status_code == 403
    # El Service Worker y el manifest no tienen datos: el navegador los sigue pudiendo pedir.
    assert client.get(reverse("field_sw")).status_code == 200
    assert client.get(reverse("field_manifest")).status_code == 200


def test_mfa_is_required_for_the_shell_but_not_for_worker_and_manifest(client, tenant_a, settings):
    settings.MFA_ENFORCE = True
    with tenant_context(tenant_a.org):
        update_mfa_policy(membership=tenant_a.admin, mfa_policy=Organization.MfaPolicy.EVERYONE)
    login(client, tenant_a.tech)
    response = client.get(reverse("field_shell"))
    assert response.status_code == 302 and response["Location"] == reverse("mfa_activate_totp")
    assert client.get(reverse("field_sw")).status_code == 200
    assert client.get(reverse("field_manifest")).status_code == 200


def test_writes_are_not_accepted(client, tenant_a):
    login(client, tenant_a.tech)
    for name in ("field_shell", "field_sw", "field_manifest", "field_demo"):
        assert client.post(reverse(name)).status_code == 405, name
