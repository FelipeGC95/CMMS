"""REQ-02: el QR de A nunca crea solicitudes en B y la página pública nunca muestra datos de B."""

import pytest
from django.core.cache import cache
from django.urls import reverse

from apps.core.tenancy import tenant_context
from apps.work.models import WorkRequest
from apps.work.services import report_anonymous_failure
from tests.conftest import login


@pytest.fixture(autouse=True)
def _fast_form(settings, db):
    settings.QR_REPORT_MIN_SECONDS = 0
    cache.clear()


def enable(*tenants):
    for tenant in tenants:
        tenant.org.allow_anonymous_reports = True
        tenant.org.save()


def post_report(client, tenant):
    url = reverse("asset_qr", args=[tenant.asset.qr_token])
    form = client.get(url).context["form"]
    return client.post(
        url,
        {
            "name": "Juan",
            "description": "Pierde aceite",
            "answer": form._challenge["a"] + form._challenge["b"],
            "challenge": form.initial["challenge"],
        },
    )


def test_qr_of_a_creates_the_request_only_in_a(client, tenant_a, tenant_b):
    enable(tenant_a, tenant_b)
    assert post_report(client, tenant_a).status_code == 302
    with tenant_context(tenant_a.org):
        assert WorkRequest.objects.get().asset_id == tenant_a.asset.pk
    with tenant_context(tenant_b.org):
        assert not WorkRequest.objects.exists()


def test_public_page_of_a_never_shows_b(client, tenant_a, tenant_b):
    enable(tenant_a, tenant_b)
    tenant_b.org.name = "Empresa Secreta B"
    tenant_b.org.save()
    url = reverse("asset_qr", args=[tenant_a.asset.qr_token])
    for response in (client.get(url), client.get(reverse("asset_qr_sent", args=[tenant_a.asset.qr_token]))):
        html = response.content.decode()
        assert tenant_a.org.name in html
        assert "Empresa Secreta B" not in html and str(tenant_b.asset.pk) not in html


def test_b_disabled_is_not_opened_by_a_enabled(client, tenant_a, tenant_b):
    enable(tenant_a)  # solo A lo permite
    response = client.get(reverse("asset_qr", args=[tenant_b.asset.qr_token]))
    assert response.status_code == 302 and reverse("account_login") in response["Location"]
    assert client.get(reverse("asset_qr_sent", args=[tenant_b.asset.qr_token])).status_code == 404
    assert client.get(reverse("asset_qr_logo", args=[tenant_b.asset.qr_token])).status_code == 404


def test_service_refuses_an_asset_of_another_organization(tenant_a, tenant_b):
    from apps.accounts.permissions import PermissionDeniedError

    enable(tenant_a, tenant_b)
    with tenant_context(tenant_a.org), pytest.raises(PermissionDeniedError):
        report_anonymous_failure(asset=tenant_b.asset, name="Juan", description="x")
    with tenant_context(tenant_b.org):
        assert not WorkRequest.objects.exists()


def test_inbox_photo_of_b_is_not_reachable_from_a(client, tenant_a, tenant_b):
    enable(tenant_b)
    with tenant_context(tenant_b.org):
        request = report_anonymous_failure(asset=tenant_b.asset, name="Juan", description="x")
    login(client, tenant_a.admin)
    assert client.get(reverse("request_photo", args=[request.pk])).status_code == 404
    assert "Juan" not in client.get(reverse("request_list")).content.decode()
