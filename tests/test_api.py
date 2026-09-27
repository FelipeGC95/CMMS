"""API REST v1 (X-05): tokens, endpoints, idempotencia, errores y OpenAPI."""

import datetime
import uuid

import pytest
from django.core.management import call_command
from django.urls import reverse
from django.utils import timezone

from apps.api.models import ApiToken
from apps.api.services import hash_token
from apps.assets.models import Meter, MeterKind, MeterReading
from apps.audit.models import AuditEvent
from apps.core.tenancy import tenant_context
from apps.organizations.models import Location
from apps.work.models import WorkOrder, WorkRequest
from tests.api_utils import api_client, make_token

pytestmark = pytest.mark.django_db


@pytest.fixture
def meter(tenant_a):
    with tenant_context(tenant_a.org):
        return Meter.objects.create(asset=tenant_a.asset, kind=MeterKind.HOURS)


def test_token_is_stored_hashed_and_shown_once(tenant_a):
    token, raw = make_token(tenant_a)
    assert raw.startswith(f"gbt_{token.prefix}_")
    assert token.token_hash == hash_token(raw) and raw not in token.token_hash
    assert not token.service_user.is_active and not token.service_user.has_usable_password()
    with tenant_context(tenant_a.org):
        assert AuditEvent.objects.filter(target_id=str(token.pk), action="create").exists()


def test_request_marks_last_used(tenant_a):
    client = api_client(tenant_a)
    assert client.get("/api/v1/assets/").status_code == 200
    assert ApiToken.all_tenants.get().last_used_at is not None


def test_list_and_filter_assets(tenant_a):
    client = api_client(tenant_a, "read")
    body = client.get("/api/v1/assets/", {"code": "cmp-01"}).json()
    assert body["count"] == 1 and body["results"][0]["code"] == "CMP-01"
    assert client.get("/api/v1/assets/", {"status": "stopped"}).json()["count"] == 0
    assert client.get(f"/api/v1/assets/{tenant_a.asset.pk}/").json()["asset_class_code"] == "CMP"


def test_create_and_update_asset_through_services(tenant_a):
    client = api_client(tenant_a)
    response = client.post(
        "/api/v1/assets/",
        {"code": "CMP-02", "name": "Compresor 2", "asset_class": str(tenant_a.asset.asset_class_id)},
        format="json",
    )
    assert response.status_code == 201, response.content
    pk = response.json()["id"]
    response = client.patch(f"/api/v1/assets/{pk}/", {"name": "Compresor dos"}, format="json")
    assert response.status_code == 200 and response.json()["name"] == "Compresor dos"
    with tenant_context(tenant_a.org):
        event = AuditEvent.objects.filter(target_id=pk, action="create").get()
    assert event.actor_type == AuditEvent.ActorType.API_TOKEN


def test_duplicate_code_is_a_spanish_validation_error(tenant_a):
    response = api_client(tenant_a).post(
        "/api/v1/assets/",
        {"code": "CMP-01", "name": "Otro", "asset_class": str(tenant_a.asset.asset_class_id)},
        format="json",
    )
    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == "invalid" and error["message"] == "Revisa los datos enviados."
    assert "code" in error["details"]


def test_reading_create_is_idempotent_by_client_uuid(tenant_a, meter):
    client = api_client(tenant_a)
    payload = {"meter": str(meter.pk), "value": "120.5", "client_uuid": str(uuid.uuid4())}
    first = client.post("/api/v1/readings/", payload, format="json")
    second = client.post("/api/v1/readings/", payload, format="json")
    assert first.status_code == second.status_code == 201
    assert first.json()["id"] == second.json()["id"]
    with tenant_context(tenant_a.org):
        reading = MeterReading.objects.get()
    assert reading.source == MeterReading.Source.API
    assert client.get("/api/v1/readings/", {"meter": str(meter.pk)}).json()["count"] == 1
    assert client.get("/api/v1/meters/").json()["results"][0]["current_value"] == "120.500"


def test_lower_cumulative_reading_is_rejected(tenant_a, meter):
    client = api_client(tenant_a)
    client.post("/api/v1/readings/", {"meter": str(meter.pk), "value": "100"}, format="json")
    response = client.post("/api/v1/readings/", {"meter": str(meter.pk), "value": "50"}, format="json")
    assert response.status_code == 400
    assert "menor que la actual" in str(response.json()["error"]["details"])


def test_bulk_readings_report_per_row_and_are_idempotent(tenant_a, meter):
    client = api_client(tenant_a)
    rows = [
        {"asset_code": "CMP-01", "value": "10", "read_at": "2026-09-20 08:00"},
        {"asset_code": "CMP-01", "value": "20", "read_at": "2026-09-21 08:00"},
        {"asset_code": "NOPE", "value": "5", "read_at": "2026-09-21 08:00"},
    ]
    body = client.post("/api/v1/readings/bulk/", {"rows": rows}, format="json").json()
    assert (body["created"], body["duplicates"], body["errors"]) == (2, 0, 1)
    assert [r["result"] for r in body["rows"]] == ["create", "create", "error"]
    again = client.post("/api/v1/readings/bulk/", {"rows": rows[:2]}, format="json").json()
    assert (again["created"], again["duplicates"]) == (0, 2)
    with tenant_context(tenant_a.org):
        assert MeterReading.objects.filter(source=MeterReading.Source.API).count() == 2


def test_work_order_create_is_idempotent_and_transitions(tenant_a):
    client = api_client(tenant_a)
    payload = {
        "work_type": "corrective",
        "title": "Fuga",
        "asset": str(tenant_a.asset.pk),
        "client_uuid": str(uuid.uuid4()),
    }
    first = client.post("/api/v1/work-orders/", payload, format="json")
    assert first.status_code == 201, first.content
    assert client.post("/api/v1/work-orders/", payload, format="json").json()["id"] == first.json()["id"]
    pk = first.json()["id"]
    for _ in range(2):  # repetir el mismo destino no falla (reintentos)
        response = client.post(f"/api/v1/work-orders/{pk}/transition/", {"to_status": "in_progress"})
        assert response.status_code == 200 and response.json()["status"] == "in_progress"
    bad = client.post(f"/api/v1/work-orders/{pk}/transition/", {"to_status": "requested"})
    assert bad.status_code == 400 and "Transición no permitida" in str(bad.json()["error"]["details"])
    listed = client.get("/api/v1/work-orders/", {"status": "in_progress", "asset": str(tenant_a.asset.pk)})
    assert listed.json()["count"] == 1
    with tenant_context(tenant_a.org):
        assert WorkOrder.objects.count() == 1


def test_work_order_needs_asset_xor_location(tenant_a):
    with tenant_context(tenant_a.org):
        site = Location.objects.create(code="S1", name="Planta")
    response = api_client(tenant_a).post(
        "/api/v1/work-orders/",
        {"work_type": "corrective", "title": "x", "asset": str(tenant_a.asset.pk), "location": str(site.pk)},
        format="json",
    )
    assert response.status_code == 400


def test_work_request_create_is_idempotent(tenant_a):
    client = api_client(tenant_a)
    payload = {"asset": str(tenant_a.asset.pk), "description": "Ruido", "client_uuid": str(uuid.uuid4())}
    first = client.post("/api/v1/requests/", payload, format="json")
    second = client.post("/api/v1/requests/", payload, format="json")
    assert first.status_code == 201 and first.json()["id"] == second.json()["id"]
    assert first.json()["work_order_code"].startswith("OT-C-")
    assert client.get("/api/v1/requests/", {"status": "pending"}).json()["count"] == 1
    with tenant_context(tenant_a.org):
        assert WorkRequest.objects.count() == 1


def test_unknown_id_is_404_json(tenant_a):
    response = api_client(tenant_a).get(f"/api/v1/assets/{uuid.uuid4()}/")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_invalid_filter_is_400(tenant_a):
    response = api_client(tenant_a).get("/api/v1/assets/", {"location": "nope"})
    assert response.status_code == 400 and "location" in response.json()["error"]["details"]


def test_pagination_page_size(tenant_a):
    body = api_client(tenant_a).get("/api/v1/assets/", {"page_size": 1}).json()
    assert set(body) == {"count", "next", "previous", "results"}


def test_throttle_limits_per_token(tenant_a, settings):
    from rest_framework.settings import api_settings
    from rest_framework.throttling import SimpleRateThrottle

    rates = {"api_token": "2/min"}
    SimpleRateThrottle.THROTTLE_RATES = rates
    try:
        client = api_client(tenant_a)
        codes = [client.get("/api/v1/assets/").status_code for _ in range(3)]
        other = api_client(tenant_a)
        assert codes == [200, 200, 429]
        assert other.get("/api/v1/assets/").status_code == 200  # el límite es por token
    finally:
        SimpleRateThrottle.THROTTLE_RATES = api_settings.DEFAULT_THROTTLE_RATES


def test_expiry_in_future_works(tenant_a):
    client = api_client(tenant_a, expires_at=timezone.now() + datetime.timedelta(days=1))
    assert client.get("/api/v1/assets/").status_code == 200


def test_openapi_schema_and_docs(client):
    call_command(
        "spectacular", "--validate", "--fail-on-warn", "--file", "NUL" if _windows() else "/dev/null"
    )
    schema = client.get(reverse("api_schema"), HTTP_ACCEPT="application/vnd.oai.openapi+json")
    assert schema.status_code == 200 and b"/api/v1/work-orders/" in schema.content
    docs = client.get(reverse("api_docs"))
    assert docs.status_code == 200
    assert b"<script>" not in docs.content  # el inicio va en un archivo aparte (CSP)


def _windows():
    import os

    return os.name == "nt"
