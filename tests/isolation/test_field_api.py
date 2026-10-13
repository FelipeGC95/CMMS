"""API de terreno (`/field/api/`): la organización B no ve, edita ni referencia datos de A."""

import uuid

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

from apps.accounts.models import Membership
from apps.api.models import ApiIdempotencyKey
from apps.assets.models import Meter, MeterKind
from apps.core.middleware import SESSION_KEY
from apps.core.tenancy import tenant_context
from apps.work.models import FailureCode, LaborEntry, WorkOrder, WorkOrderChecklistItem, WorkRequest
from apps.work.services import create_work_order
from tests.api_utils import FIELD_API, field_client
from tests.conftest import tiny_jpeg_bytes

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _media(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path


def _wo(tenant):
    with tenant_context(tenant.org):
        wo = create_work_order(work_type="improvement", title="OT", asset=tenant.asset)
        item = WorkOrderChecklistItem.objects.create(work_order=wo, text="Revisar", item_type="check")
        meter = Meter.objects.create(asset=tenant.asset, kind=MeterKind.HOURS)
        FailureCode.objects.create(
            kind=FailureCode.Kind.MODE, code=f"M-{tenant.org.slug[-1].upper()}", name="Modo"
        )
    return wo, item, meter


def test_reads_only_bring_the_active_organization(tenant_a, tenant_b):
    wo_a, _item_a, meter_a = _wo(tenant_a)
    wo_b, _item_b, meter_b = _wo(tenant_b)
    client = field_client(tenant_b.tech)
    bootstrap = client.get(f"{FIELD_API}/bootstrap/").json()
    assert bootstrap["organization"]["id"] == str(tenant_b.org.pk)
    ids = {wo["id"] for wo in client.get(f"{FIELD_API}/snapshot/").json()["work_orders"]}
    assert ids == {str(wo_b.pk)}
    reference = client.get(f"{FIELD_API}/reference/").json()
    assert {a["id"] for a in reference["assets"]} == {str(tenant_b.asset.pk)}
    assert {m["id"] for m in reference["meters"]} == {str(meter_b.pk)}
    codes = {c["code"] for kind in reference["failure_codes"].values() for c in kind}
    assert "M-A" not in codes and "M-B" in codes
    meters = {m["id"] for m in client.get(f"{FIELD_API}/meters/").json()["results"]}
    assert meters == {str(meter_b.pk)}
    assert client.get(f"{FIELD_API}/meters/{meter_a.pk}/").status_code == 404
    assert client.get(f"{FIELD_API}/work-orders/{wo_a.pk}/checklist-items/").status_code == 404


def test_b_cannot_touch_work_orders_of_a(tenant_a, tenant_b):
    wo_a, item_a, _meter = _wo(tenant_a)
    _wo(tenant_b)
    client = field_client(tenant_b.tech)
    base = f"{FIELD_API}/work-orders/{wo_a.pk}"
    jpeg = SimpleUploadedFile("f.jpg", tiny_jpeg_bytes(), content_type="image/jpeg")
    writes = [
        (f"{base}/transition/", {"to_status": "in_progress"}, "json"),
        (f"{base}/complete/", {"labor_minutes": 10}, "json"),
        (f"{base}/labor/", {"minutes": 10}, "json"),
        (f"{base}/checklist-items/{item_a.pk}/answer/", {"result": "ok"}, "json"),
        (f"{base}/checklist-items/{item_a.pk}/photos/", {"file": jpeg}, "multipart"),
        (f"{base}/photos/", {"file": jpeg}, "multipart"),
        (f"{base}/parts/", {"description": "Huaipe", "quantity": "1"}, "json"),
        (f"{base}/checklist-items/{item_a.pk}/finding/", {"description": "Fuga"}, "json"),
        (f"{base}/checklist/{item_a.pk}/finding/", {"description": "Fuga"}, "json"),  # ruta anterior
    ]
    with tenant_context(tenant_a.org):
        WorkOrderChecklistItem.objects.filter(pk=item_a.pk).update(result="not_ok")
    for url, body, fmt in writes:
        assert client.post(url, body, format=fmt).status_code == 404, url
    with tenant_context(tenant_a.org):
        wo = WorkOrder.objects.get(pk=wo_a.pk)
        assert wo.status == WorkOrder.Status.PLANNED and not LaborEntry.objects.exists()
        assert WorkOrderChecklistItem.objects.get(pk=item_a.pk).result == "not_ok"
        assert not WorkOrder.objects.filter(source_checklist_item=item_a).exists()


def test_b_cannot_reference_assets_or_meters_of_a(tenant_a, tenant_b):
    _wo_a, _item_a, meter_a = _wo(tenant_a)
    client = field_client(tenant_b.tech)
    body = {"description": "Falla", "asset": str(tenant_a.asset.pk)}
    response = client.post(f"{FIELD_API}/requests/", body, format="json")
    assert response.status_code == 400 and "asset" in response.json()["error"]["details"]
    response = client.post(f"{FIELD_API}/readings/", {"meter": str(meter_a.pk), "value": "5"}, format="json")
    assert response.status_code == 400 and "meter" in response.json()["error"]["details"]
    with tenant_context(tenant_a.org):
        assert not WorkRequest.objects.exists() and not meter_a.readings.exists()


def test_client_uuid_of_a_does_not_collide_in_b(tenant_a, tenant_b):
    """La misma clave en otra organización es otra operación: ni conflicto ni respuesta de A."""
    wo_a, _i, _m = _wo(tenant_a)
    wo_b, _i, _m = _wo(tenant_b)
    key = str(uuid.uuid4())
    for tenant, wo in ((tenant_a, wo_a), (tenant_b, wo_b)):
        client = field_client(tenant.tech)
        response = client.post(
            f"{FIELD_API}/work-orders/{wo.pk}/transition/",
            {"to_status": "in_progress", "client_uuid": key},
            format="json",
        )
        assert response.status_code == 200 and response.json()["id"] == str(wo.pk)
        labor = client.post(
            f"{FIELD_API}/work-orders/{wo.pk}/labor/", {"minutes": 5, "client_uuid": key}, format="json"
        )
        assert labor.status_code == 201
    for tenant in (tenant_a, tenant_b):
        with tenant_context(tenant.org):
            assert ApiIdempotencyKey.objects.filter(client_uuid=key).count() == 1


def test_session_uses_the_active_organization_of_a_person_in_both(tenant_a, tenant_b):
    """Quien pertenece a A y B ve solo la organización abierta en su sesión."""
    wo_a, _i, _m = _wo(tenant_a)
    wo_b, _i, _m = _wo(tenant_b)
    Membership.objects.create(user=tenant_a.tech.user, organization=tenant_b.org, role="technician")
    api = field_client(tenant_a.tech)
    session = api.session
    session[SESSION_KEY] = str(tenant_b.org.pk)
    session.save()
    assert api.get(f"{FIELD_API}/bootstrap/").json()["organization"]["id"] == str(tenant_b.org.pk)
    ids = {wo["id"] for wo in api.get(f"{FIELD_API}/snapshot/").json()["work_orders"]}
    assert ids == {str(wo_b.pk)}
    response = api.post(
        f"{FIELD_API}/work-orders/{wo_a.pk}/transition/", {"to_status": "in_progress"}, format="json"
    )
    assert response.status_code == 404


def test_changes_made_in_another_organization_are_refused_after_a_switch(tenant_a, tenant_b):
    """La cola guarda la organización de cada cambio (X-Field-Organization). Si la sesión ya es
    de otra, el envío no se aplica ni se marca como conflicto: 409 `organization_changed`."""
    wo_b, _i, meter_b = _wo(tenant_b)
    Membership.objects.create(user=tenant_a.tech.user, organization=tenant_b.org, role="technician")
    api = field_client(tenant_a.tech)
    session = api.session
    session[SESSION_KEY] = str(tenant_b.org.pk)
    session.save()
    stale = {"HTTP_X_FIELD_ORGANIZATION": str(tenant_a.org.pk)}
    labor = api.post(f"{FIELD_API}/work-orders/{wo_b.pk}/labor/", {"minutes": 5}, format="json", **stale)
    assert labor.status_code == 409
    assert labor.json()["error"]["code"] == "organization_changed"
    reading = api.post(
        f"{FIELD_API}/readings/", {"meter": str(meter_b.pk), "value": "10"}, format="json", **stale
    )
    assert reading.status_code == 409
    with tenant_context(tenant_b.org):
        assert not LaborEntry.objects.exists()
    current = {"HTTP_X_FIELD_ORGANIZATION": str(tenant_b.org.pk)}
    labor = api.post(f"{FIELD_API}/work-orders/{wo_b.pk}/labor/", {"minutes": 5}, format="json", **current)
    assert labor.status_code == 201


def test_finding_cannot_mix_work_orders_and_items_of_two_organizations(tenant_a, tenant_b):
    """El ítem de A no se alcanza por una OT de B (ni al revés): 404 y nada se crea."""
    _wo_a, item_a, _m = _wo(tenant_a)
    wo_b, item_b, _m = _wo(tenant_b)
    for tenant, item in ((tenant_a, item_a), (tenant_b, item_b)):
        with tenant_context(tenant.org):
            WorkOrderChecklistItem.objects.filter(pk=item.pk).update(result="not_ok")
    client = field_client(tenant_b.tech)
    for route in ("checklist-items", "checklist"):  # la ruta canónica y la anterior
        url = f"{FIELD_API}/work-orders/{wo_b.pk}/{route}/{item_a.pk}/finding/"
        assert client.post(url, {}, format="json").status_code == 404
    with tenant_context(tenant_a.org):
        assert not WorkOrder.objects.filter(source_checklist_item=item_a).exists()
        assert not WorkRequest.objects.exists()
    # Su propio ítem sí: la correctiva y la solicitud quedan en B.
    own = client.post(
        f"{FIELD_API}/work-orders/{wo_b.pk}/checklist-items/{item_b.pk}/finding/", {}, format="json"
    )
    assert own.status_code == 201
    with tenant_context(tenant_b.org):
        corrective = WorkOrder.objects.get(pk=own.json()["id"])
        assert corrective.organization_id == tenant_b.org.pk
        assert WorkRequest.objects.get(work_order=corrective).organization_id == tenant_b.org.pk
