"""API v1 (X-05): un token de A nunca lee, escribe ni referencia datos de B."""

import io

import pytest
from django.urls import reverse

from apps.api.models import ApiToken
from apps.assets.models import Meter, MeterKind, MeterReading
from apps.core.tenancy import tenant_context
from apps.organizations.models import Location
from apps.work.models import WorkOrder, WorkOrderChecklistItem, WorkRequest
from apps.work.services import create_work_order, report_failure
from tests.api_utils import api_client, make_token
from tests.conftest import login

pytestmark = pytest.mark.django_db


@pytest.fixture
def b_data(tenant_b):
    with tenant_context(tenant_b.org):
        meter = Meter.objects.create(asset=tenant_b.asset, kind=MeterKind.HOURS)
        location = Location.objects.create(code="SB", name="Planta B")
        work_order = create_work_order(work_type="corrective", title="B", asset=tenant_b.asset)
        request = report_failure(membership=tenant_b.admin, asset=tenant_b.asset, description="B")
        reading = MeterReading.objects.create(meter=meter, value=1, read_at=work_order.created_at)
    return {
        "assets": tenant_b.asset.pk,
        "meters": meter.pk,
        "readings": reading.pk,
        "work-orders": work_order.pk,
        "requests": request.pk,
        "location": location.pk,
        "meter": meter,
    }


@pytest.mark.parametrize("resource", ["assets", "meters", "readings", "work-orders", "requests"])
def test_lists_only_show_own_organization(tenant_a, b_data, resource):
    body = api_client(tenant_a).get(f"/api/v1/{resource}/").json()
    ids = {row["id"] for row in body["results"]}
    assert str(b_data[resource]) not in ids


@pytest.mark.parametrize("resource", ["assets", "meters", "readings", "work-orders", "requests"])
def test_retrieve_by_id_of_other_organization_is_404(tenant_a, b_data, resource):
    assert api_client(tenant_a).get(f"/api/v1/{resource}/{b_data[resource]}/").status_code == 404


def test_filters_by_other_organization_ids_return_nothing(tenant_a, b_data):
    client = api_client(tenant_a)
    assert client.get("/api/v1/assets/", {"location": str(b_data["location"])}).json()["count"] == 0
    assert client.get("/api/v1/assets/", {"code": "CMP-01"}).json()["results"][0]["id"] != str(
        b_data["assets"]
    )
    assert client.get("/api/v1/meters/", {"asset": str(b_data["assets"])}).json()["count"] == 0
    assert client.get("/api/v1/readings/", {"meter": str(b_data["meters"])}).json()["count"] == 0
    assert client.get("/api/v1/work-orders/", {"asset": str(b_data["assets"])}).json()["count"] == 0
    assert client.get("/api/v1/work-orders/", {"location": str(b_data["location"])}).json()["count"] == 0


def test_writes_cannot_touch_or_reference_other_organization(tenant_a, tenant_b, b_data):
    client = api_client(tenant_a)
    assert (
        client.patch(f"/api/v1/assets/{b_data['assets']}/", {"name": "x"}, format="json").status_code == 404
    )
    assert (
        client.post(
            f"/api/v1/work-orders/{b_data['work-orders']}/transition/", {"to_status": "cancelled"}
        ).status_code
        == 404
    )
    references = [
        ("/api/v1/readings/", {"meter": str(b_data["meters"]), "value": "5"}),
        ("/api/v1/work-orders/", {"work_type": "corrective", "title": "x", "asset": str(b_data["assets"])}),
        (
            "/api/v1/work-orders/",
            {"work_type": "corrective", "title": "x", "location": str(b_data["location"])},
        ),
        ("/api/v1/requests/", {"description": "x", "asset": str(b_data["assets"])}),
        (
            "/api/v1/assets/",
            {"code": "N1", "name": "N", "asset_class": str(tenant_b.asset.asset_class_id)},
        ),
        (
            "/api/v1/assets/",
            {
                "code": "N2",
                "name": "N",
                "asset_class": str(tenant_a.asset.asset_class_id),
                "location": str(b_data["location"]),
            },
        ),
    ]
    for url, payload in references:
        assert client.post(url, payload, format="json").status_code == 400, (url, payload)
    bulk = client.post(
        "/api/v1/readings/bulk/",
        {"rows": [{"meter": str(b_data["meters"]), "value": "9", "read_at": "2026-09-20"}]},
        format="json",
    ).json()
    assert bulk["created"] == 0 and bulk["errors"] == 1
    with tenant_context(tenant_b.org):
        assert MeterReading.objects.count() == 1
        assert WorkOrder.objects.count() == 2 and WorkRequest.objects.count() == 1


def test_client_uuid_of_other_organization_does_not_return_its_object(tenant_a, tenant_b, b_data):
    with tenant_context(tenant_b.org):
        work_order = WorkOrder.objects.get(pk=b_data["work-orders"])
        work_order.client_uuid = "0190a000-0000-7000-8000-000000000001"
        work_order.save()
    response = api_client(tenant_a).post(
        "/api/v1/work-orders/",
        {
            "work_type": "corrective",
            "title": "A",
            "asset": str(tenant_a.asset.pk),
            "client_uuid": str(work_order.client_uuid),
        },
        format="json",
    )
    assert response.status_code == 201 and response.json()["id"] != str(work_order.pk)


def test_token_screen_lists_and_revokes_only_own_tokens(client, tenant_a, tenant_b):
    token_b, _raw = make_token(tenant_b)
    login(client, tenant_a.admin)
    assert token_b.prefix not in client.get(reverse("api_token_list")).content.decode()
    assert client.post(reverse("api_token_revoke", args=[token_b.pk])).status_code == 404
    assert ApiToken.all_tenants.get(pk=token_b.pk).revoked_at is None


def test_cannot_complete_or_answer_other_organization_work_orders(tenant_a, tenant_b, b_data):
    wo_b = b_data["work-orders"]
    with tenant_context(tenant_b.org):
        item_b = WorkOrderChecklistItem.objects.create(work_order_id=wo_b, text="B", item_type="check")
    with tenant_context(tenant_a.org):
        wo_a = create_work_order(work_type="corrective", title="A", asset=tenant_a.asset)
    client = api_client(tenant_a)
    payload = {"labor_minutes": 5, "impact": "none"}
    assert client.post(f"/api/v1/work-orders/{wo_b}/complete/", payload, format="json").status_code == 404
    for wo in (wo_b, wo_a.pk):  # ni por la OT de B ni colgando el ítem de B de una OT de A
        url = f"/api/v1/work-orders/{wo}/checklist-items/{item_b.pk}/answer/"
        assert client.post(url, {"result": "ok"}).status_code == 404
    with tenant_context(tenant_b.org):
        assert WorkOrder.objects.get(pk=wo_b).status not in ("completed", "closed")
        assert WorkOrderChecklistItem.objects.get(pk=item_b.pk).result == ""


def test_completion_key_of_other_organization_is_ignored(tenant_a, tenant_b, b_data):
    key = "0190a000-0000-7000-8000-000000000002"
    payload = {"labor_minutes": 5, "impact": "none", "client_uuid": key}
    assert (
        api_client(tenant_b)
        .post(f"/api/v1/work-orders/{b_data['work-orders']}/complete/", payload, format="json")
        .status_code
        == 200
    )
    with tenant_context(tenant_a.org):
        wo_a = create_work_order(work_type="corrective", title="A", asset=tenant_a.asset)
    response = api_client(tenant_a).post(f"/api/v1/work-orders/{wo_a.pk}/complete/", payload, format="json")
    assert response.status_code == 200 and response.json()["id"] == str(wo_a.pk)
    assert response.json()["completed_at"] is not None


def test_csv_import_does_not_see_other_organization_assets(client, tenant_a, b_data):
    login(client, tenant_a.admin)
    csv = io.BytesIO(f"meter_id,value,read_at\n{b_data['meters']},5,2026-09-20\n".encode())
    csv.name = "l.csv"
    response = client.post(reverse("reading_import"), {"file": csv})
    assert response.context["counts"] == {"create": 0, "duplicate": 0, "error": 1}
