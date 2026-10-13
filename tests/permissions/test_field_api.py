"""API de terreno (`/field/api/`, ADR-011): sesiÃ³n, MFA, mÃ³dulos del plan, rol y alcance por sitio."""

import uuid

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework.test import APIClient

from apps.accounts.models import MembershipLocation, User
from apps.assets.models import Asset, Meter, MeterKind
from apps.core.tenancy import tenant_context
from apps.organizations.models import Location, Organization, Plan
from apps.organizations.services import update_mfa_policy
from apps.work.models import WorkOrder, WorkOrderChecklistItem
from apps.work.services import create_work_order
from tests.api_utils import FIELD_API, api_client, field_client
from tests.conftest import PASSWORD, tiny_jpeg_bytes

pytestmark = pytest.mark.django_db

READ_URLS = ["bootstrap/", "snapshot/", "reference/", "meters/"]


@pytest.fixture(autouse=True)
def _media(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path


def _wo(tenant, asset=None):
    with tenant_context(tenant.org):
        return create_work_order(work_type="improvement", title="OT", asset=asset or tenant.asset)


def _item(tenant, wo):
    with tenant_context(tenant.org):
        return WorkOrderChecklistItem.objects.create(work_order=wo, text="Revisar", item_type="check")


def _writes(tenant, wo, item):
    """Cada escritura de ejecuciÃ³n de OT con un cuerpo vÃ¡lido."""
    base = f"{FIELD_API}/work-orders/{wo.pk}"
    jpeg = SimpleUploadedFile("f.jpg", tiny_jpeg_bytes(), content_type="image/jpeg")
    return [
        (f"{base}/transition/", {"to_status": "in_progress"}, "json"),
        (f"{base}/complete/", {"labor_minutes": 10}, "json"),
        (f"{base}/labor/", {"minutes": 10}, "json"),
        (f"{base}/checklist-items/{item.pk}/answer/", {"result": "ok"}, "json"),
        (f"{base}/photos/", {"file": jpeg}, "multipart"),
        (f"{base}/parts/", {"description": "Huaipe", "quantity": "1"}, "json"),
    ]


@pytest.mark.parametrize("url", [*READ_URLS, "requests/", "readings/"])
def test_without_session_is_401(tenant_a, url):
    client = APIClient()
    response = client.get(f"{FIELD_API}/{url}") if url in READ_URLS else client.post(f"{FIELD_API}/{url}", {})
    assert response.status_code == 401
    assert response["WWW-Authenticate"] == 'Session realm="field"'
    assert response.json()["error"]["code"] == "not_authenticated"


def test_api_token_is_not_accepted(tenant_a):
    """La API de terreno es solo para la sesiÃ³n de la web: un token de integraciÃ³n no entra."""
    client = api_client(tenant_a)
    assert client.get(f"{FIELD_API}/bootstrap/").status_code == 401


def test_user_without_membership_is_403(db):
    client = APIClient()
    client.force_login(User.objects.create_user("suelto@x.cl", PASSWORD))
    response = client.get(f"{FIELD_API}/bootstrap/")
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "permission_denied"


def test_mfa_required_answers_json_instead_of_redirecting(tenant_a, settings):
    settings.MFA_ENFORCE = True
    with tenant_context(tenant_a.org):
        update_mfa_policy(membership=tenant_a.admin, mfa_policy=Organization.MfaPolicy.EVERYONE)
    client = field_client(tenant_a.tech)
    wo = _wo(tenant_a)
    for response in (
        client.get(f"{FIELD_API}/snapshot/"),
        client.post(
            f"{FIELD_API}/work-orders/{wo.pk}/transition/", {"to_status": "in_progress"}, format="json"
        ),
    ):
        assert response.status_code == 403
        assert response["Content-Type"] == "application/json"
        assert response.json()["error"]["code"] == "mfa_required"
    with tenant_context(tenant_a.org):
        assert WorkOrder.objects.get(pk=wo.pk).status == WorkOrder.Status.PLANNED


def _plan(tenant, features):
    tenant.org.plan = Plan.objects.create(code="-".join(features) or "nada", name="Plan", features=features)
    tenant.org.save(update_fields=["plan"])


def test_plan_without_work_orders_blocks_work_order_endpoints(tenant_a):
    _plan(tenant_a, ["assets", "requests"])
    client = field_client(tenant_a.tech)
    for url in READ_URLS:
        response = client.get(f"{FIELD_API}/{url}")
        assert response.status_code == 403, url
        assert response["Content-Type"] == "application/json"  # JSON, no la pÃ¡gina de plan
    wo = _wo(tenant_a)
    response = client.post(f"{FIELD_API}/work-orders/{wo.pk}/labor/", {"minutes": 5}, format="json")
    assert response.status_code == 403
    # Avisar fallas es del mÃ³dulo `requests`, que el plan sÃ­ incluye.
    body = {"description": "Gotea", "asset": str(tenant_a.asset.pk)}
    assert client.post(f"{FIELD_API}/requests/", body, format="json").status_code == 201


def test_plan_without_requests_blocks_requests(tenant_a):
    _plan(tenant_a, ["assets", "work_orders"])
    client = field_client(tenant_a.tech)
    body = {"description": "Gotea", "asset": str(tenant_a.asset.pk)}
    assert client.post(f"{FIELD_API}/requests/", body, format="json").status_code == 403
    assert client.get(f"{FIELD_API}/snapshot/").status_code == 200


def test_the_api_module_is_not_required(tenant_a):
    _plan(tenant_a, ["assets", "work_orders", "requests"])
    client = field_client(tenant_a.tech)
    for url in READ_URLS:
        assert client.get(f"{FIELD_API}/{url}").status_code == 200, url
    # La API v1 sÃ­ lo exige.
    assert api_client(tenant_a).get("/api/v1/work-orders/").status_code == 403


def test_viewer_cannot_execute_work_orders(tenant_a):
    wo = _wo(tenant_a)
    item = _item(tenant_a, wo)
    client = field_client(tenant_a.viewer)
    for url, body, fmt in _writes(tenant_a, wo, item):
        response = client.post(url, body, format=fmt)
        assert response.status_code == 403, url
    with tenant_context(tenant_a.org):
        assert WorkOrder.objects.get(pk=wo.pk).status == WorkOrder.Status.PLANNED
        meter = Meter.objects.create(asset=tenant_a.asset, kind=MeterKind.HOURS)
    # Pero sÃ­ avisa fallas y registra lecturas (como en la web).
    body = {"description": "Gotea", "asset": str(tenant_a.asset.pk)}
    assert client.post(f"{FIELD_API}/requests/", body, format="json").status_code == 201
    reading = {"meter": str(meter.pk), "value": "10"}
    assert client.post(f"{FIELD_API}/readings/", reading, format="json").status_code == 201


def test_external_cannot_record_readings(tenant_a):
    with tenant_context(tenant_a.org):
        meter = Meter.objects.create(asset=tenant_a.asset, kind=MeterKind.HOURS)
    user = User.objects.create_user("externo@x.cl", PASSWORD)
    external = tenant_a.org.memberships.create(user=user, role="external")
    client = field_client(external)
    response = client.post(f"{FIELD_API}/readings/", {"meter": str(meter.pk), "value": "10"}, format="json")
    assert response.status_code in (400, 403)  # el externo ni siquiera ve el medidor
    with tenant_context(tenant_a.org):
        assert not meter.readings.exists()
    assert client.get(f"{FIELD_API}/bootstrap/").json()["permissions"]["reading_create"] is False


@pytest.fixture
def scoped(tenant_a):
    """El tÃ©cnico queda limitado a Planta Norte; la OT de Planta Sur no es suya."""
    with tenant_context(tenant_a.org):
        north = Location.objects.create(code="N", name="Planta Norte")
        south = Location.objects.create(code="S", name="Planta Sur")
        cls = tenant_a.asset.asset_class
        inside = Asset.objects.create(code="IN-01", name="Llenadora", asset_class=cls, location=north)
        outside = Asset.objects.create(code="OUT-01", name="Caldera", asset_class=cls, location=south)
        Meter.objects.create(asset=outside, kind=MeterKind.HOURS)
        MembershipLocation.objects.create(membership=tenant_a.tech, location=north)
    tenant_a.tech.restricted_to_locations = True
    tenant_a.tech.save()
    return inside, outside


def test_technician_only_downloads_work_orders_in_scope(tenant_a, scoped):
    inside, outside = scoped
    wo_in, wo_out = _wo(tenant_a, inside), _wo(tenant_a, outside)
    client = field_client(tenant_a.tech)
    ids = {wo["id"] for wo in client.get(f"{FIELD_API}/snapshot/").json()["work_orders"]}
    assert str(wo_in.pk) in ids and str(wo_out.pk) not in ids
    reference = client.get(f"{FIELD_API}/reference/").json()
    assert {a["code"] for a in reference["assets"]} == {"IN-01"}
    assert reference["meters"] == []


def test_technician_cannot_execute_work_orders_out_of_scope(tenant_a, scoped):
    _inside, outside = scoped
    wo = _wo(tenant_a, outside)
    item = _item(tenant_a, wo)
    client = field_client(tenant_a.tech)
    for url, body, fmt in _writes(tenant_a, wo, item):
        assert client.post(url, body, format=fmt).status_code == 404, url
    assert client.get(f"{FIELD_API}/work-orders/{wo.pk}/checklist-items/").status_code == 404
    # Tampoco avisa fallas ni registra lecturas de activos fuera de su alcance.
    body = {"description": "x", "asset": str(outside.pk), "client_uuid": str(uuid.uuid4())}
    assert client.post(f"{FIELD_API}/requests/", body, format="json").status_code == 400
    with tenant_context(tenant_a.org):
        meter = Meter.objects.get(asset=outside)
    response = client.post(f"{FIELD_API}/readings/", {"meter": str(meter.pk), "value": "1"}, format="json")
    assert response.status_code == 400


# --- Hallazgo sin conexión (WO-06 CA2) -------------------------------------------------------


def _not_ok_item(tenant, wo):
    with tenant_context(tenant.org):
        return WorkOrderChecklistItem.objects.create(
            work_order=wo, text="Revisar", item_type="check", result="not_ok"
        )


def _finding_url(wo, item, route="checklist-items"):
    return f"{FIELD_API}/work-orders/{wo.pk}/{route}/{item.pk}/finding/"


def test_finding_needs_request_create_like_the_web(tenant_a):
    """Como en la web (`request.create`): quien no gestiona OT avisa la falla, también el lector."""
    wo = _wo(tenant_a)
    item = _not_ok_item(tenant_a, wo)
    response = field_client(tenant_a.viewer).post(_finding_url(wo, item), {}, format="json")
    assert response.status_code == 201 and response.json()["request_number"] is not None
    user = User.objects.create_user("externo-hallazgo@x.cl", PASSWORD)
    external = tenant_a.org.memberships.create(user=user, role="external")
    other = _not_ok_item(tenant_a, wo)
    assert field_client(external).post(_finding_url(wo, other), {}, format="json").status_code in (403, 404)
    with tenant_context(tenant_a.org):
        assert not WorkOrder.objects.filter(source_checklist_item=other).exists()


@pytest.mark.parametrize("route", ["checklist-items", "checklist"])
def test_finding_needs_the_work_orders_module(tenant_a, route):
    wo = _wo(tenant_a)
    item = _not_ok_item(tenant_a, wo)
    _plan(tenant_a, ["assets", "requests"])
    response = field_client(tenant_a.tech).post(_finding_url(wo, item, route), {}, format="json")
    assert response.status_code == 403


@pytest.mark.parametrize("route", ["checklist-items", "checklist"])
def test_finding_out_of_scope_is_404(tenant_a, scoped, route):
    _inside, outside = scoped
    wo = _wo(tenant_a, outside)
    item = _not_ok_item(tenant_a, wo)
    response = field_client(tenant_a.tech).post(_finding_url(wo, item, route), {}, format="json")
    assert response.status_code == 404
    with tenant_context(tenant_a.org):
        assert not WorkOrder.objects.filter(source_checklist_item=item).exists()
