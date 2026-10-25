"""Tokens de API limitados por sitio (X-05, P-05, ADR-034): el token ve y toca solo lo que
cuelga de sus sitios, con los mismos selectores que el alcance por sitio de una membresía.
Un token sin sitios (todos los existentes) sigue viendo toda la organización."""

import pytest
from django.urls import reverse
from rest_framework.test import APIClient

from apps.api.models import ApiToken, ApiTokenSite
from apps.api.services import TokenError, create_token
from apps.assets.models import Asset
from apps.core.tenancy import tenant_context
from apps.organizations.models import Location
from apps.work.services import create_work_order, report_failure
from tests.conftest import login

pytestmark = pytest.mark.django_db


@pytest.fixture
def sites(tenant_a):
    """Dos sitios: el activo del tenant en Norte (dentro de una línea) y otro activo en Sur."""
    with tenant_context(tenant_a.org):
        north = Location.objects.create(code="NOR", name="Planta Norte", kind=Location.Kind.SITE)
        line = Location.objects.create(code="NOR-L1", name="Línea 1", kind=Location.Kind.LINE, parent=north)
        south = Location.objects.create(code="SUR", name="Planta Sur", kind=Location.Kind.SITE)
        Asset.objects.filter(pk=tenant_a.asset.pk).update(location=line)
        other = Asset.objects.create(
            code="CMP-02",
            name="Compresor 2",
            asset_class=tenant_a.asset.asset_class,
            criticality="B",
            location=south,
        )
        data = {
            "north": north,
            "line": line,
            "south": south,
            "asset_in": tenant_a.asset,
            "asset_out": other,
            "wo_in": create_work_order(work_type="corrective", title="Norte", asset=tenant_a.asset),
            "wo_out": create_work_order(work_type="corrective", title="Sur", asset=other),
            "req_in": report_failure(membership=tenant_a.admin, description="Norte", asset=tenant_a.asset),
            "req_out": report_failure(membership=tenant_a.admin, description="Sur", asset=other),
        }
    return data


def _client(tenant, sites=(), scopes=("*:read", "*:write")):
    with tenant_context(tenant.org):
        token, raw = create_token(membership=tenant.admin, name="Planta", scopes=list(scopes), sites=sites)
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {raw}")
    return token, client


def _ids(response):
    assert response.status_code == 200, response.content
    return {row["id"] for row in response.json()["results"]}


def test_site_token_only_lists_what_hangs_from_its_sites(tenant_a, sites):
    _token, client = _client(tenant_a, sites=[sites["north"]])
    assert _ids(client.get("/api/v1/assets/")) == {str(sites["asset_in"].pk)}
    assert _ids(client.get("/api/v1/work-orders/")) == {
        str(sites["wo_in"].pk),
        str(sites["req_in"].work_order_id),
    }
    assert _ids(client.get("/api/v1/requests/")) == {str(sites["req_in"].pk)}
    assert client.get(f"/api/v1/assets/{sites['asset_out'].pk}/").status_code == 404
    assert client.get(f"/api/v1/work-orders/{sites['wo_out'].pk}/").status_code == 404
    assert client.get(f"/api/v1/requests/{sites['req_out'].pk}/").status_code == 404
    # Filtrar por una ubicación de otro sitio no revela nada.
    assert _ids(client.get(f"/api/v1/work-orders/?location={sites['south'].pk}")) == set()


def test_site_token_cannot_write_outside_its_sites(tenant_a, sites):
    _token, client = _client(tenant_a, sites=[sites["north"]])
    outside = {"work_type": "corrective", "title": "Fuera", "asset": str(sites["asset_out"].pk)}
    assert client.post("/api/v1/work-orders/", outside, format="json").status_code == 400
    by_location = {"work_type": "corrective", "title": "Fuera", "location": str(sites["south"].pk)}
    assert client.post("/api/v1/work-orders/", by_location, format="json").status_code == 400
    request = {"description": "Fuera", "location": str(sites["south"].pk)}
    assert client.post("/api/v1/requests/", request, format="json").status_code == 400
    asset = {
        "code": "X-1",
        "name": "X",
        "asset_class": str(sites["asset_in"].asset_class_id),
        "criticality": "C",
    }
    assert (
        client.post(
            "/api/v1/assets/", {**asset, "location": str(sites["south"].pk)}, format="json"
        ).status_code
        == 400
    )
    assert client.post("/api/v1/assets/", asset, format="json").status_code == 400  # sin ubicación
    # Dentro de su sitio (también en una ubicación hija) sí.
    inside = {"work_type": "corrective", "title": "Dentro", "location": str(sites["line"].pk)}
    assert client.post("/api/v1/work-orders/", inside, format="json").status_code == 201
    assert (
        client.post(
            "/api/v1/assets/", {**asset, "location": str(sites["line"].pk)}, format="json"
        ).status_code
        == 201
    )


def test_token_without_sites_keeps_seeing_the_whole_organization(tenant_a, sites):
    token, client = _client(tenant_a)
    assert token.restricted_to_sites is False
    assert _ids(client.get("/api/v1/assets/")) == {str(sites["asset_in"].pk), str(sites["asset_out"].pk)}
    by_location = {"work_type": "corrective", "title": "Sur", "location": str(sites["south"].pk)}
    assert client.post("/api/v1/work-orders/", by_location, format="json").status_code == 201


def test_several_sites_add_up(tenant_a, sites):
    _token, client = _client(tenant_a, sites=[sites["north"], sites["south"]])
    assert _ids(client.get("/api/v1/assets/")) == {str(sites["asset_in"].pk), str(sites["asset_out"].pk)}


def test_only_active_sites_can_restrict_a_token(tenant_a, sites):
    with tenant_context(tenant_a.org):
        with pytest.raises(TokenError):
            create_token(membership=tenant_a.admin, name="X", scopes=["*:read"], sites=[sites["line"]])
        Location.objects.filter(pk=sites["south"].pk).update(is_active=False)
        sites["south"].refresh_from_db()
        with pytest.raises(TokenError):
            create_token(membership=tenant_a.admin, name="X", scopes=["*:read"], sites=[sites["south"]])
        assert not ApiToken.objects.filter(name="X").exists()


def test_screen_creates_a_site_token_and_lists_its_sites(client, tenant_a, sites):
    login(client, tenant_a.admin)
    html = client.get(reverse("api_token_list")).content.decode()
    assert 'name="sites"' in html and "Planta Norte" in html and "Línea 1" not in html
    response = client.post(
        reverse("api_token_list"), {"name": "Planta", "scopes": ["*:read"], "sites": [str(sites["north"].pk)]}
    )
    assert response.status_code == 200
    with tenant_context(tenant_a.org):
        token = ApiToken.objects.get(name="Planta")
        assert token.restricted_to_sites
        assert list(ApiTokenSite.objects.filter(token=token).values_list("location_id", flat=True)) == [
            sites["north"].pk
        ]
    # La lista muestra el sitio del token (y no «Toda la organización»).
    tokens_table = response.content.decode().split('<h2 class="mt-4">')[-1]
    assert "Planta Norte" in tokens_table and "Toda la organización" not in tokens_table


def test_screen_rejects_a_site_of_another_organization(client, tenant_a, tenant_b, sites):
    with tenant_context(tenant_b.org):
        foreign = Location.objects.create(code="BSITE", name="Planta B", kind=Location.Kind.SITE)
    login(client, tenant_a.admin)
    response = client.post(
        reverse("api_token_list"), {"name": "Planta", "scopes": ["*:read"], "sites": [str(foreign.pk)]}
    )
    assert response.status_code == 200 and 'class="error"' in response.content.decode()
    with tenant_context(tenant_a.org):
        assert not ApiToken.objects.filter(name="Planta").exists()
