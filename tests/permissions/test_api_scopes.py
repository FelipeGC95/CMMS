"""Alcances de token por recurso (X-05, ADR-034): lectura, escritura, grupos y tokens antiguos."""

import pytest
from django.urls import reverse
from rest_framework.test import APIClient

from apps.api import scopes
from apps.api.models import ApiToken
from apps.api.services import TokenError, create_token
from apps.core.tenancy import tenant_context
from tests.api_utils import api_client
from tests.conftest import login

pytestmark = pytest.mark.django_db


def _client(tenant, scopes_):
    with tenant_context(tenant.org):
        _token, raw = create_token(membership=tenant.admin, name="ERP", scopes=scopes_)
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {raw}")
    return client


def _new_request(tenant):
    return {"description": "Ruido", "asset": str(tenant.asset.pk)}


def test_group_read_scope_reads_only_its_group(tenant_a):
    client = _client(tenant_a, ["assets:read"])
    assert client.get("/api/v1/assets/").status_code == 200
    response = client.get("/api/v1/work-orders/")
    assert response.status_code == 403
    assert response.json()["error"] == {
        "code": "insufficient_scope",
        "message": "El token no tiene el alcance «work_orders:read» que pide esta operación.",
        "details": {"required_scope": "work_orders:read"},
    }


def test_read_only_token_does_not_write(tenant_a):
    client = _client(tenant_a, ["requests:read"])
    assert client.get("/api/v1/requests/").status_code == 200
    response = client.post("/api/v1/requests/", _new_request(tenant_a), format="json")
    assert response.status_code == 403
    assert response.json()["error"]["details"] == {"required_scope": "requests:write"}


def test_write_scope_implies_read_of_the_same_group(tenant_a):
    client = _client(tenant_a, ["requests:write"])
    assert client.get("/api/v1/requests/").status_code == 200
    assert client.post("/api/v1/requests/", _new_request(tenant_a), format="json").status_code == 201
    assert client.get("/api/v1/assets/").status_code == 403


def test_token_without_inventory_scope_gets_403(tenant_a):
    client = _client(tenant_a, ["assets:write", "work_orders:write"])
    for url in ("/api/v1/parts/", "/api/v1/stock/", "/api/v1/stock-movements/", "/api/v1/warehouses/"):
        response = client.get(url)
        assert response.status_code == 403, url
        assert response.json()["error"]["details"] == {"required_scope": "inventory:read"}
    assert _client(tenant_a, ["inventory:read"]).get("/api/v1/parts/").status_code == 200


def test_condition_and_meter_groups(tenant_a):
    client = _client(tenant_a, ["meters:read"])
    assert client.get("/api/v1/meters/").status_code == 200
    assert client.get("/api/v1/readings/").status_code == 200
    assert client.get("/api/v1/condition-alerts/").status_code == 403
    assert _client(tenant_a, ["condition:read"]).get("/api/v1/condition-alerts/").status_code == 200


def test_global_scopes_cover_every_group(tenant_a):
    reader = _client(tenant_a, ["*:read"])
    for url in ("/api/v1/assets/", "/api/v1/work-orders/", "/api/v1/parts/", "/api/v1/condition-rules/"):
        assert reader.get(url).status_code == 200, url
    assert reader.post("/api/v1/requests/", _new_request(tenant_a), format="json").status_code == 403
    writer = _client(tenant_a, ["*:write"])
    assert writer.post("/api/v1/requests/", _new_request(tenant_a), format="json").status_code == 201


def test_legacy_shortcuts_keep_working(tenant_a):
    """`create_token(scope=...)` y los tokens migrados: lectura = todo lectura, escritura = todo."""
    read = api_client(tenant_a, "read")
    assert read.get("/api/v1/parts/").status_code == 200
    assert read.post("/api/v1/requests/", _new_request(tenant_a), format="json").status_code == 403
    write = api_client(tenant_a, "write")
    assert write.post("/api/v1/requests/", _new_request(tenant_a), format="json").status_code == 201
    assert sorted(ApiToken.all_tenants.values_list("scopes", flat=True), key=len) == [
        ["*:read"],
        ["*:read", "*:write"],
    ]


def test_scopes_are_required_and_cleaned(tenant_a):
    with tenant_context(tenant_a.org):
        with pytest.raises(TokenError):
            create_token(membership=tenant_a.admin, name="x", scopes=["nope:read"])
        token, _raw = create_token(
            membership=tenant_a.admin, name="x", scopes=["inventory:write", "bogus", "*:read", "*:read"]
        )
    assert token.scopes == ["*:read", "inventory:write"]


def test_read_only_billing_still_answers_402(tenant_a, monkeypatch):
    """Con alcance de escritura, la organización en solo lectura sigue respondiendo 402."""
    from apps.billing import access

    monkeypatch.setattr(
        access, "billing_status", lambda organization, **kw: access.BillingStatus(access.Access.READ_ONLY)
    )
    response = _client(tenant_a, ["*:write"]).post("/api/v1/requests/", _new_request(tenant_a), format="json")
    assert response.status_code == 402


def test_required_scope_mapping():
    assert scopes.required_scope(basename="api-stock-movement", method="POST") == "inventory:write"
    assert scopes.required_scope(basename="api-reading", method="GET") == "meters:read"
    assert scopes.required_scope(basename="field-work-order", method="GET") is None
    assert scopes.allows(["*:write"], group="assets", write=False)
    assert not scopes.allows(["assets:read"], group="assets", write=True)


def test_openapi_declares_required_scope(client):
    schema = client.get(reverse("api_schema"), {"format": "json"}).json()
    assert schema["paths"]["/api/v1/parts/"]["post"]["x-required-scope"] == "inventory:write"
    assert schema["paths"]["/api/v1/assets/"]["get"]["x-required-scope"] == "assets:read"
    assert "assets:read" in schema["paths"]["/api/v1/assets/"]["get"]["description"]


def test_token_screen_shows_scope_matrix_without_plan_modules(client, tenant_a):
    from apps.organizations.models import Plan

    tenant_a.org.plan = Plan.objects.create(
        code="scopes-test", name="Pro", features=["api", "assets", "work_orders", "requests"]
    )
    tenant_a.org.save()
    login(client, tenant_a.admin)
    html = client.get(reverse("api_token_list")).content.decode()
    assert 'value="*:read"' in html and 'value="work_orders:write"' in html
    assert 'value="inventory:read"' not in html  # el plan no incluye inventario
    response = client.post(reverse("api_token_list"), {"name": "ERP"})
    assert "Elige al menos un alcance." in response.content.decode()
