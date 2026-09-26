"""Páginas con sesión sin caché del navegador (ORG-07 CA2)."""

from django.http import HttpResponse
from django.test import RequestFactory
from django.urls import reverse

from apps.accounts.models import Membership
from apps.core.middleware import PrivateCacheMiddleware
from tests.conftest import login

NO_STORE = "no-store, private"


def test_authenticated_pages_are_not_stored(client, tenant_a):
    login(client, tenant_a.admin)
    for url in [
        reverse("dashboard"),
        reverse("asset_list"),
        reverse("menu"),
        reverse("organization_settings"),
    ]:
        assert client.get(url)["Cache-Control"] == NO_STORE, url


def test_switching_organization_is_not_cached(client, tenant_a, tenant_b):
    Membership.objects.create(user=tenant_a.admin.user, organization=tenant_b.org, role="viewer")
    login(client, tenant_a.admin)
    response = client.post(reverse("switch_organization"), {"organization": str(tenant_b.org.pk)})
    assert response["Cache-Control"] == NO_STORE
    assert client.get(reverse("dashboard"))["Cache-Control"] == NO_STORE


def test_anonymous_and_health_responses_are_untouched(client, tenant_a):
    assert client.get(reverse("account_login")).get("Cache-Control") != NO_STORE
    login(client, tenant_a.admin)
    assert "Cache-Control" not in client.get(reverse("healthz"))


def test_static_and_media_are_skipped(tenant_a):
    middleware = PrivateCacheMiddleware(lambda request: HttpResponse("x"))
    for path in ["/static/css/tokens.css", "/media/org/x.jpg"]:
        request = RequestFactory().get(path)
        request.user = tenant_a.admin.user
        assert "Cache-Control" not in middleware(request), path
    request = RequestFactory().get("/assets/")
    request.user = tenant_a.admin.user
    assert middleware(request)["Cache-Control"] == NO_STORE
