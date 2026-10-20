"""Solo lectura central (ORG-13 CA5): bloquea escrituras en web, API v1 y /field/api/; permite
facturación, exportar, salir y cambiar de organización; exentas nunca se bloquean. Webhook (CA6)."""

import datetime
import hashlib
import hmac
import json

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.billing import http
from apps.billing.models import BillingAccount, ProviderEvent
from apps.core.tenancy import tenant_context
from apps.organizations.models import Organization, Plan
from tests.api_utils import FIELD_API, api_client, field_client
from tests.billing_utils import make_billable
from tests.conftest import login


def _lock(tenant):
    """Prueba vencida hace 30 días (gracia agotada), con el plan Profesional (API y exportaciones)."""
    make_billable(tenant.org)
    Organization.objects.filter(pk=tenant.org.pk).update(
        trial_ends_at=timezone.now() - datetime.timedelta(days=30), plan=Plan.objects.get(code="pro")
    )


@pytest.fixture
def locked(tenant_a):
    _lock(tenant_a)
    return tenant_a


def test_web_writes_are_blocked_with_a_clear_message(client, locked):
    login(client, locked.admin)
    response = client.post(reverse("location_list"), {"code": "S1", "name": "Planta", "kind": "site"})
    assert response.status_code == 402
    assert "solo lectura" in response.content.decode()
    assert reverse("billing") in response.content.decode()  # el admin ve dónde pagar
    # Leer sigue permitido, con el aviso.
    page = client.get(reverse("asset_list"))
    assert page.status_code == 200 and "solo lectura por falta de pago" in page.content.decode()


def test_htmx_writes_redirect_back_with_the_message(client, locked):
    login(client, locked.tech)
    response = client.post(
        reverse("location_list"), {}, HTTP_HX_REQUEST="true", HTTP_HX_CURRENT_URL="http://testserver/assets/"
    )
    assert response.status_code == 402 and response["HX-Redirect"] == "http://testserver/assets/"


def test_non_admin_does_not_get_the_billing_link(client, locked):
    login(client, locked.tech)
    response = client.post(reverse("location_list"), {})
    assert response.status_code == 402
    assert reverse("billing") not in response.content.decode()


@pytest.mark.parametrize(
    "url_name", ["billing", "data_export_request", "account_logout", "switch_organization"]
)
def test_whitelisted_writes_still_work(client, locked, url_name):
    login(client, locked.admin)
    response = client.post(reverse(url_name), {})
    assert response.status_code != 402


def test_exports_still_work(client, locked):
    login(client, locked.admin)
    assert client.get(reverse("asset_export_xlsx")).status_code == 200


def test_exempt_organizations_are_never_blocked(client, tenant_a):
    Organization.objects.filter(pk=tenant_a.org.pk).update(
        trial_ends_at=timezone.now() - datetime.timedelta(days=90)
    )
    login(client, tenant_a.admin)
    response = client.post(reverse("location_list"), {"code": "S1", "name": "Planta", "kind": "site"})
    assert response.status_code == 302


def test_grace_still_writes_and_warns_admins(client, tenant_a):
    make_billable(tenant_a.org)
    Organization.objects.filter(pk=tenant_a.org.pk).update(
        trial_ends_at=timezone.now() - datetime.timedelta(days=2)
    )
    login(client, tenant_a.admin)
    response = client.post(reverse("location_list"), {"code": "S1", "name": "Planta", "kind": "site"})
    assert response.status_code == 302
    assert "Tu prueba terminó" in client.get(reverse("dashboard")).content.decode()
    login(client, tenant_a.tech)
    assert "Tu prueba terminó" not in client.get(reverse("dashboard")).content.decode()


def test_trial_ending_banner_for_admins(client, tenant_a):
    make_billable(tenant_a.org, trial_days=3)
    login(client, tenant_a.admin)
    assert "Tu prueba gratuita termina" in client.get(reverse("dashboard")).content.decode()


def test_api_v1_writes_answer_402(locked):
    client = api_client(locked)
    response = client.post("/api/v1/requests/", {"description": "Fuga", "asset": str(locked.asset.pk)})
    assert response.status_code == 402
    assert response.json()["error"]["code"] == "payment_required"
    assert client.get("/api/v1/assets/").status_code == 200


def test_field_api_writes_answer_402(locked):
    client = field_client(locked.tech)
    response = client.post(f"{FIELD_API}/requests/", {"description": "Fuga"}, format="json")
    assert response.status_code == 402
    assert response.json()["error"]["code"] == "payment_required"
    assert client.get(f"{FIELD_API}/bootstrap/").status_code == 200


def test_deletion_grace_is_read_only_but_can_be_cancelled(client, tenant_a):
    Organization.objects.filter(pk=tenant_a.org.pk).update(
        status=Organization.Status.PENDING_DELETION,
        deletion_scheduled_for=timezone.now() + datetime.timedelta(days=30),
    )
    login(client, tenant_a.admin)
    assert client.post(reverse("location_list"), {}).status_code == 402
    assert client.post(reverse("organization_delete_cancel")).status_code == 302
    assert Organization.objects.get(pk=tenant_a.org.pk).status == Organization.Status.ACTIVE


def test_anonymous_qr_report_is_closed_while_read_only(client, locked):
    Organization.objects.filter(pk=locked.org.pk).update(allow_anonymous_reports=True)
    with tenant_context(locked.org):
        token = locked.asset.qr_token
    response = client.get(reverse("asset_qr", args=[token]))
    assert response.status_code == 302 and "login" in response["Location"]


# --- Webhook entrante (ORG-13 CA6) ------------------------------------------------------------

SECRET = "secreto-de-firma"


def _signed(client, data_id, *, kind="subscription_preapproval", secret=SECRET, body=None, ts="1700000000"):
    manifest = f"id:{data_id.lower()};request-id:req-1;ts:{ts};"
    signature = hmac.new(secret.encode(), manifest.encode(), hashlib.sha256).hexdigest()
    return client.post(
        f"/billing/webhooks/mercadopago/?data.id={data_id}&type={kind}",
        data=body if body is not None else json.dumps({"id": 77, "type": kind, "data": {"id": data_id}}),
        content_type="application/json",
        HTTP_X_SIGNATURE=f"ts={ts},v1={signature}",
        HTTP_X_REQUEST_ID="req-1",
    )


@pytest.fixture
def mp(db, settings, monkeypatch):
    settings.MERCADOPAGO_ACCESS_TOKEN = "TEST-token"
    settings.MERCADOPAGO_WEBHOOK_SECRET = SECRET
    responses = {}
    calls = []

    def fake(method, url, *, headers=None, payload=None, **kwargs):
        calls.append((method, url, headers))
        for path, response in responses.items():
            if url.endswith(path):
                return response
        return 404, {"message": "not found"}

    monkeypatch.setattr(http, "request_json", fake)
    return responses, calls


def test_webhook_rejects_an_invalid_signature(client, mp):
    assert _signed(client, "pre1", secret="otro").status_code == 401
    assert client.post("/billing/webhooks/mercadopago/?data.id=pre1").status_code == 401
    assert ProviderEvent.objects.count() == 0


def test_webhook_queries_the_api_and_activates_the_subscription(client, tenant_a, mp):
    responses, calls = mp
    make_billable(tenant_a.org)
    with tenant_context(tenant_a.org):
        account = BillingAccount.objects.create(
            plan=tenant_a.org.plan.__class__.objects.get(code="basic"),
            status=BillingAccount.Status.PENDING,
            provider_subscription_id="pre1",
        )
    responses["/preapproval/pre1"] = (
        200,
        {
            "id": "pre1",
            "status": "authorized",
            "external_reference": account.external_reference,
            "auto_recurring": {"transaction_amount": 59250},
            "next_payment_date": "2026-11-19T10:00:00.000-03:00",
        },
    )
    # El cuerpo dice "cancelled": no se le cree, manda la API.
    body = json.dumps(
        {"id": 77, "type": "subscription_preapproval", "data": {"id": "pre1"}, "status": "cancelled"}
    )
    response = _signed(client, "pre1", body=body)
    assert response.status_code == 200 and response.json()["status"] == "processed"
    assert calls[0][0] == "GET" and calls[0][2]["Authorization"] == "Bearer TEST-token"
    with tenant_context(tenant_a.org):
        assert BillingAccount.objects.get().status == BillingAccount.Status.AUTHORIZED
    org = Organization.objects.get(pk=tenant_a.org.pk)
    assert org.plan.code == "basic" and org.status == Organization.Status.ACTIVE
    # Repetido: no se vuelve a consultar.
    assert _signed(client, "pre1", body=body).json()["status"] == "duplicate"
    assert len(calls) == 1


def test_webhook_never_answers_500(client, mp):
    responses, _calls = mp
    assert _signed(client, "x1", kind="otro_tipo").status_code == 200
    assert _signed(client, "x2", kind="otro_tipo", body="no es json").status_code == 200
    responses["/preapproval/x3"] = (200, {"id": "x3", "status": "rarísimo", "external_reference": None})
    assert _signed(client, "x3").status_code == 200


def test_webhook_retries_when_the_provider_is_down(client, mp):
    assert _signed(client, "pre-caido").status_code == 503


def test_webhook_disabled_without_credentials(client, db):
    assert client.post("/billing/webhooks/mercadopago/").status_code == 503
