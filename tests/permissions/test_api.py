"""API v1 (X-05): alcances de token, tokens revocados/vencidos y pantalla solo para administradores."""

import datetime
import io

import pytest
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import Membership
from apps.accounts.permissions import Perm, PermissionDeniedError, has_perm
from apps.api.models import ApiToken
from apps.api.services import create_token, revoke_token
from apps.core.tenancy import platform_context, tenant_context
from tests.api_utils import api_client, make_token
from tests.conftest import login

pytestmark = pytest.mark.django_db
Role = Membership.Role


@pytest.mark.parametrize("role", Role.values)
def test_only_admin_manages_api_tokens(role):
    assert has_perm(Membership(role=role), Perm.API_TOKEN_MANAGE) is (role == Role.ADMIN)


def test_service_refuses_non_admin(tenant_a):
    with tenant_context(tenant_a.org), pytest.raises(PermissionDeniedError):
        create_token(membership=tenant_a.tech, name="x", scope="read")


@pytest.mark.parametrize("role", ["planner", "supervisor", "technician", "viewer", "external"])
def test_token_screen_is_admin_only(client, tenant_a, role):
    token, _raw = make_token(tenant_a)
    member = tenant_a.tech
    member.role = role
    member.save()
    login(client, member)
    assert client.get(reverse("api_token_list")).status_code == 403
    assert client.post(reverse("api_token_list"), {"name": "x", "scope": "write"}).status_code == 403
    assert client.post(reverse("api_token_revoke", args=[token.pk])).status_code == 403


def test_admin_creates_token_shown_once_and_revokes(client, tenant_a):
    login(client, tenant_a.admin)
    response = client.post(reverse("api_token_list"), {"name": "ERP", "scope": "read", "expires_in": "30"})
    raw = response.context["raw_token"]
    assert raw and raw in response.content.decode()
    token = ApiToken.all_tenants.get()
    assert token.expires_at is not None and token.scope == "read"
    assert raw not in client.get(reverse("api_token_list")).content.decode()
    assert reverse("api_token_list") in client.get(reverse("menu")).content.decode()
    client.post(reverse("api_token_revoke", args=[token.pk]))
    # Fuera del contexto de la petición la fila solo se ve con bypass de RLS (ADR-026).
    with platform_context():
        token.refresh_from_db()
    assert token.revoked_at is not None


def test_read_scope_cannot_write(tenant_a):
    client = api_client(tenant_a, "read")
    assert client.get("/api/v1/work-orders/").status_code == 200
    response = client.post(
        "/api/v1/requests/", {"description": "x", "asset": str(tenant_a.asset.pk)}, format="json"
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "permission_denied"
    assert client.post("/api/v1/readings/bulk/", {"rows": []}, format="json").status_code == 403


def test_missing_invalid_revoked_and_expired_tokens_are_401(tenant_a):
    assert APIClient().get("/api/v1/assets/").status_code == 401
    bad = APIClient()
    bad.credentials(HTTP_AUTHORIZATION="Bearer gbt_nope_nope")
    assert bad.get("/api/v1/assets/").status_code == 401

    token, raw = make_token(tenant_a)
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {raw}")
    assert client.get("/api/v1/assets/").status_code == 200
    with tenant_context(tenant_a.org):
        revoke_token(membership=tenant_a.admin, token=token)
    response = client.get("/api/v1/assets/")
    assert response.status_code == 401 and response.json()["error"]["code"] == "authentication_failed"

    expired = api_client(tenant_a, expires_at=timezone.now() - datetime.timedelta(seconds=1))
    assert expired.get("/api/v1/assets/").status_code == 401


def test_inactive_organization_token_is_401(tenant_a):
    client = api_client(tenant_a)
    tenant_a.org.is_active = False
    tenant_a.org.save()
    assert client.get("/api/v1/assets/").status_code == 401


def test_session_does_not_authenticate_the_api(client, tenant_a):
    login(client, tenant_a.admin)
    assert client.get("/api/v1/assets/").status_code == 401


def test_service_principal_cannot_manage_team_or_tokens(tenant_a):
    from apps.api.services import principal_for

    token, _raw = make_token(tenant_a)
    principal = principal_for(token)
    assert not has_perm(principal, Perm.TEAM_MANAGE)
    assert not has_perm(principal, Perm.API_TOKEN_MANAGE)
    assert has_perm(principal, Perm.WORK_ORDER_MANAGE)


@pytest.mark.parametrize("role", ["supervisor", "technician", "viewer", "external"])
def test_csv_reading_import_is_for_asset_managers(client, tenant_a, role):
    member = tenant_a.tech
    member.role = role
    member.save()
    login(client, member)
    assert client.get(reverse("reading_import")).status_code == 403
    csv = io.BytesIO(b"asset_code,value,read_at\nCMP-01,5,2026-09-20\n")
    csv.name = "l.csv"
    assert client.post(reverse("reading_import"), {"file": csv}).status_code == 403
    assert client.post(reverse("reading_import_confirm", args=["x"])).status_code == 403
