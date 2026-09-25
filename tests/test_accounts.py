"""Acceso con allauth (ADR-006), organización recordada (ORG-07) y planes de plataforma."""

import re

import pytest
from allauth.account.models import EmailAddress
from django.contrib.auth.models import Permission
from django.core import mail
from django.urls import reverse

from apps.accounts.models import Membership, User
from apps.accounts.services import create_user
from apps.organizations.models import Plan
from tests.conftest import PASSWORD, login


def verify(user):
    EmailAddress.objects.create(user=user, email=user.email, primary=True, verified=True)
    return user


def sign_in(client, email, password=PASSWORD):
    return client.post(reverse("account_login"), {"login": email, "password": password})


def is_signed_in(client):
    return "_auth_user_id" in client.session


def test_login_with_email_and_password(client, tenant_a):
    user = verify(tenant_a.admin.user)
    response = sign_in(client, user.email.upper())
    assert response.status_code == 302 and response["Location"] == reverse("dashboard")
    assert client.get(reverse("dashboard")).context["organization"] == tenant_a.org


def test_wrong_password_is_rejected(client, tenant_a):
    user = verify(tenant_a.admin.user)
    response = sign_in(client, user.email, "otra-clave-999")
    assert response.status_code == 200
    assert not is_signed_in(client)


def test_unverified_email_must_be_confirmed_before_entering(client, tenant_a):
    user = tenant_a.tech.user  # sin EmailAddress: como un alta hecha en el admin
    sign_in(client, user.email)
    assert not is_signed_in(client)
    assert len(mail.outbox) == 1 and mail.outbox[0].to == [user.email]

    confirm_url = re.search(r"https?://[^/\s]+(/accounts/confirm-email/\S+)", mail.outbox[0].body).group(1)
    client.post(confirm_url)
    assert EmailAddress.objects.get(user=user).verified
    assert is_signed_in(client)


def test_signup_is_closed(client, db):
    response = client.post(
        reverse("account_signup"), {"email": "nuevo@empresa.cl", "password1": "una-clave-larga-123"}
    )
    assert "account/signup_closed.html" in [t.name for t in response.templates]
    assert not User.objects.filter(email="nuevo@empresa.cl").exists()


def test_logout(client, tenant_a):
    login(client, tenant_a.admin)
    client.post(reverse("account_logout"))
    assert not is_signed_in(client)


def test_admin_login_goes_through_allauth(client, db):
    response = client.get("/admin/login/")
    assert response.status_code == 302 and response["Location"].startswith(reverse("account_login"))


def test_create_user_hashes_with_argon2_and_registers_email(db):
    user = create_user(email="Ana@Empresa.cl", password=PASSWORD, email_verified=True)
    assert user.email == "ana@empresa.cl"
    assert user.password.startswith("argon2")
    assert EmailAddress.objects.get(user=user).verified


def test_login_reopens_last_organization(client, tenant_a, tenant_b):
    user = verify(tenant_b.admin.user)
    Membership.objects.create(user=user, organization=tenant_a.org, role="viewer")

    for org in [tenant_a.org, tenant_b.org]:
        login(client, tenant_b.admin)
        client.post(reverse("switch_organization"), {"organization": str(org.id)})
        user.refresh_from_db()
        assert user.last_organization == org
        client.logout()
        sign_in(client, user.email)
        assert client.get(reverse("dashboard")).context["organization"] == org
        client.logout()


def test_default_plans_exist(db):
    assert set(Plan.objects.values_list("code", flat=True)) >= {"trial", "basic", "pro", "enterprise"}


@pytest.mark.parametrize("url_name", ["admin:organizations_plan_changelist", "admin:organizations_plan_add"])
def test_tenant_admin_cannot_manage_plans_even_with_model_permissions(client, tenant_a, url_name):
    user = tenant_a.admin.user
    user.is_staff = True
    user.save()
    user.user_permissions.set(Permission.objects.filter(content_type__model="plan"))
    login(client, tenant_a.admin)
    assert client.get(reverse(url_name)).status_code == 403


def test_platform_admin_manages_plans(client, tenant_a):
    user = tenant_a.admin.user
    user.is_staff = user.is_superuser = True
    user.save()
    login(client, tenant_a.admin)
    assert client.get(reverse("admin:organizations_plan_changelist")).status_code == 200
