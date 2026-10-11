"""API de terreno (`/field/api/`, X-06, ADR-011): contrato, escrituras sin conexión y conflictos."""

import datetime
import json
import uuid
from decimal import Decimal

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone
from rest_framework.settings import api_settings
from rest_framework.throttling import SimpleRateThrottle

from apps.api.field import EXECUTION_TRANSITIONS
from apps.api.framework import MIN_CLIENT_VERSION
from apps.api.models import ApiIdempotencyKey
from apps.assets.models import Meter, MeterKind, MeterReading
from apps.attachments.models import Attachment, TargetType
from apps.core.tenancy import tenant_context
from apps.field.views import DEMO_FIXTURE
from apps.inventory import services as inventory
from apps.inventory.models import Reservation, StockItem, StockMovement
from apps.organizations.models import Location, Plan
from apps.work.models import (
    FailureCode,
    LaborEntry,
    PartUsage,
    WorkOrder,
    WorkOrderChecklistItem,
    WorkOrderTransition,
    WorkRequest,
)
from apps.work.services import EXECUTION_TARGETS, TRANSITIONS, add_part_usage, create_work_order
from tests.api_utils import FIELD_API, api_client, field_client
from tests.conftest import tiny_jpeg_bytes
from tests.test_field import (
    BOOTSTRAP_KEYS,
    ITEM_KEYS,
    PART_USAGE_KEYS,
    PLANNED_PART_KEYS,
    WORK_ORDER_KEYS,
)

pytestmark = pytest.mark.django_db

S = WorkOrder.Status
DAY_AGO = datetime.timedelta(days=1)


@pytest.fixture(autouse=True)
def _media(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path


def _jpeg(name="foto.jpg"):
    return SimpleUploadedFile(name, tiny_jpeg_bytes(), content_type="image/jpeg")


def _wo(tenant, *, backdate=True, **kwargs):
    """OT creada "ayer": las horas informadas por el teléfono no se acotan a su creación."""
    kwargs.setdefault("work_type", "corrective")
    kwargs.setdefault("title", "Fuga de aceite")
    kwargs.setdefault("asset", tenant.asset)
    with tenant_context(tenant.org):
        wo = create_work_order(**kwargs)
        if backdate:
            WorkOrder.objects.filter(pk=wo.pk).update(created_at=timezone.now() - DAY_AGO)
            wo.refresh_from_db()
        return wo


def _item(tenant, wo, item_type="check", **kwargs):
    with tenant_context(tenant.org):
        return WorkOrderChecklistItem.objects.create(
            work_order=wo, text="Revisar", item_type=item_type, required=True, **kwargs
        )


def _get(tenant, model, **filters):
    with tenant_context(tenant.org):
        return model.objects.get(**filters)


def _set_status(tenant, wo, status, **fields):
    with tenant_context(tenant.org):
        WorkOrder.objects.filter(pk=wo.pk).update(status=status, **fields)


def _iso(dt):
    return dt.isoformat()


def _url(wo, suffix):
    return f"{FIELD_API}/work-orders/{wo.pk}/{suffix}/"


# --- bootstrap ------------------------------------------------------------------------------


def test_bootstrap_follows_the_contract(tenant_a):
    response = field_client(tenant_a.tech).get(f"{FIELD_API}/bootstrap/")
    assert response.status_code == 200
    data = response.json()
    fixture = json.loads(DEMO_FIXTURE.read_text(encoding="utf-8"))["bootstrap"]
    assert set(data) == BOOTSTRAP_KEYS
    assert set(data["organization"]) == set(fixture["organization"])
    assert set(data["user"]) == set(fixture["user"])
    assert data["user"] == {"id": str(tenant_a.tech.user.pk), "name": str(tenant_a.tech.user)}
    assert data["organization"]["id"] == str(tenant_a.org.pk)
    assert data["organization"]["timezone"] == tenant_a.org.timezone
    assert data["role"] == "technician"
    assert data["permissions"] == {"execute": True, "request_create": True, "reading_create": True}
    assert data["horizon_days"] == 7
    assert data["min_client_version"] == MIN_CLIENT_VERSION
    assert data["server_time"].endswith("Z")
    assert set(data["status_labels"]) == set(S.values)
    assert data["execution_transitions"] == fixture["execution_transitions"]
    # El no-store de las páginas con sesión también cubre la API de terreno (ORG-07 CA2).
    assert response["Cache-Control"] == "no-store, private"


def test_bootstrap_execution_transitions_are_allowed_by_the_lifecycle():
    for to_status, from_statuses in EXECUTION_TRANSITIONS.values():
        assert to_status in EXECUTION_TARGETS
        for status in from_statuses:
            assert to_status in TRANSITIONS[status]


def test_bootstrap_uses_the_organization_status_labels(tenant_a):
    tenant_a.org.work_order_status_labels = {"in_progress": "En faena"}
    tenant_a.org.save(update_fields=["work_order_status_labels"])
    labels = field_client(tenant_a.tech).get(f"{FIELD_API}/bootstrap/").json()["status_labels"]
    assert labels["in_progress"] == "En faena"
    assert labels["on_hold"] == "En espera"


def test_bootstrap_permissions_follow_the_role(tenant_a):
    data = field_client(tenant_a.viewer).get(f"{FIELD_API}/bootstrap/").json()
    assert data["role"] == "viewer"
    assert data["permissions"] == {"execute": False, "request_create": True, "reading_create": True}


# --- snapshot -------------------------------------------------------------------------------


def test_snapshot_follows_the_contract(tenant_a):
    with tenant_context(tenant_a.org):
        Meter.objects.create(asset=tenant_a.asset, kind=MeterKind.HOURS, name="Horómetro", unit="h")
        site = Location.objects.create(code="P", name="Planta")
        room = Location.objects.create(code="SE", name="Sala eléctrica", kind="area", parent=site)
    mine = _wo(tenant_a, assigned_to=tenant_a.tech.user, title="Mía")
    _item(tenant_a, mine, item_type="measurement", unit="bar", min_value="6.5", max_value="8", order=1)
    other = _wo(tenant_a, work_type="inspection", asset=None, location=room, title="De otro", due_on=None)
    response = field_client(tenant_a.tech).get(f"{FIELD_API}/snapshot/")
    assert response.status_code == 200
    data = response.json()
    fixture = json.loads(DEMO_FIXTURE.read_text(encoding="utf-8"))["snapshot"]
    assert set(data) == set(fixture)
    assert data["days"] == 7 and data["truncated"] is False
    assert [wo["id"] for wo in data["work_orders"]] == [str(mine.pk), str(other.pk)]  # asignadas primero
    first, second = data["work_orders"]
    for wo in data["work_orders"]:
        assert set(wo) == WORK_ORDER_KEYS
    assert first["assigned_to_me"] is True and second["assigned_to_me"] is False
    assert first["assigned_to"]["id"] == str(tenant_a.tech.user.pk)
    assert first["asset"] == {"id": str(tenant_a.asset.pk), "code": "CMP-01", "name": "Compresor 1"}
    assert first["meter"]["name"] == "Horómetro" and first["meter"]["unit"] == "h"
    assert first["allowed_transitions"] == ["in_progress"]
    assert first["updated_at"].endswith("Z") and data["generated_at"].endswith("Z")
    (item,) = first["checklist"]
    assert set(item) == ITEM_KEYS
    assert item["position"] == 1 and item["photo_count"] == 0
    assert item["min_value"] == "6.500"  # decimales como texto
    assert second["location"] == {
        "id": str(room.pk),
        "name": "Sala eléctrica",
        "path": "Planta › Sala eléctrica",
    }


def test_snapshot_days_parameter(tenant_a):
    client = field_client(tenant_a.tech)
    far = _wo(tenant_a, due_on=timezone.localdate() + datetime.timedelta(days=10))
    ids = lambda r: {wo["id"] for wo in r.json()["work_orders"]}  # noqa: E731
    assert str(far.pk) not in ids(client.get(f"{FIELD_API}/snapshot/"))
    response = client.get(f"{FIELD_API}/snapshot/?days=14")
    assert response.status_code == 200 and response.json()["days"] == 14
    assert str(far.pk) in ids(response)
    for bad in ("15", "-1", "x"):
        response = client.get(f"{FIELD_API}/snapshot/?days={bad}")
        assert response.status_code == 400, bad
        assert response.json()["error"]["code"] == "invalid"


def test_snapshot_marks_truncated(tenant_a, monkeypatch):
    monkeypatch.setattr("apps.work.selectors.FIELD_WORK_ORDER_LIMIT", 1)
    _wo(tenant_a)
    _wo(tenant_a)
    data = field_client(tenant_a.tech).get(f"{FIELD_API}/snapshot/").json()
    assert data["truncated"] is True and len(data["work_orders"]) == 1


def test_reference_lists_catalogs(tenant_a):
    with tenant_context(tenant_a.org):
        meter = Meter.objects.create(asset=tenant_a.asset, kind=MeterKind.HOURS, current_value="12.5")
        FailureCode.objects.create(kind=FailureCode.Kind.MODE, code="FUG", name="Fuga")
        FailureCode.objects.create(kind=FailureCode.Kind.MODE, code="OLD", name="Vieja", is_active=False)
    response = field_client(tenant_a.tech).get(f"{FIELD_API}/reference/")
    assert response.status_code == 200
    data = response.json()
    assert set(data) == {"failure_codes", "assets", "truncated", "meters", "parts", "parts_truncated"}
    assert set(data["failure_codes"]) == set(FailureCode.Kind.values)
    modes = {c["code"] for c in data["failure_codes"][FailureCode.Kind.MODE]}
    assert "FUG" in modes and "OLD" not in modes
    assert data["assets"][0]["code"] == "CMP-01" and data["truncated"] is False
    (row,) = data["meters"]
    assert row["id"] == str(meter.pk) and row["current_value"] == "12.500"


# --- transiciones ---------------------------------------------------------------------------


def test_transition_uses_the_phone_time_and_is_idempotent(tenant_a):
    wo = _wo(tenant_a, assigned_to=tenant_a.tech.user)
    client = field_client(tenant_a.tech)
    at = timezone.now() - datetime.timedelta(hours=2)
    key = uuid.uuid4()
    body = {"to_status": "in_progress", "client_uuid": str(key), "at": _iso(at)}
    first = client.post(_url(wo, "transition"), body, format="json")
    assert first.status_code == 200 and first.json()["status"] == "in_progress"
    assert _get(tenant_a, WorkOrder, pk=wo.pk).started_at == at
    # Pausa en el teléfono y reenvío del inicio (la red cortó la respuesta): no se repite.
    client.post(
        _url(wo, "transition"), {"to_status": "on_hold", "client_uuid": str(uuid.uuid4())}, format="json"
    )
    replay = client.post(_url(wo, "transition"), body, format="json")
    assert replay.status_code == 200 and replay.json()["status"] == "on_hold"
    with tenant_context(tenant_a.org):
        assert WorkOrderTransition.objects.filter(work_order=wo, to_status="in_progress").count() == 1
        assert ApiIdempotencyKey.objects.filter(client_uuid=key).count() == 1


def test_transition_client_uuid_used_elsewhere_is_a_conflict(tenant_a):
    wo, other = _wo(tenant_a), _wo(tenant_a)
    client = field_client(tenant_a.tech)
    key = str(uuid.uuid4())
    client.post(_url(wo, "transition"), {"to_status": "in_progress", "client_uuid": key}, format="json")
    response = client.post(
        _url(other, "transition"), {"to_status": "in_progress", "client_uuid": key}, format="json"
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "conflict"


@pytest.mark.parametrize(
    ("status", "code"), [(S.CANCELLED, "cancelled"), (S.CLOSED, "locked"), (S.COMPLETED, "locked")]
)
def test_transition_on_a_finished_work_order_is_a_409(tenant_a, status, code):
    wo = _wo(tenant_a)
    _set_status(tenant_a, wo, status)
    response = field_client(tenant_a.tech).post(
        _url(wo, "transition"), {"to_status": "on_hold"}, format="json"
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == code


def test_field_transition_only_accepts_execution_states(tenant_a):
    wo = _wo(tenant_a)
    response = field_client(tenant_a.admin).post(
        _url(wo, "transition"), {"to_status": "cancelled"}, format="json"
    )
    assert response.status_code == 400
    assert _get(tenant_a, WorkOrder, pk=wo.pk).status == S.PLANNED


def test_api_v1_keeps_400_for_state_errors(tenant_a):
    """Lo aditivo de terreno no cambia la API v1: sus errores de estado siguen siendo 400."""
    wo = _wo(tenant_a)
    _set_status(tenant_a, wo, S.CANCELLED)
    response = api_client(tenant_a).post(
        f"/api/v1/work-orders/{wo.pk}/transition/", {"to_status": "in_progress"}, format="json"
    )
    assert response.status_code == 400


def test_api_v1_transition_accepts_client_uuid_and_at(tenant_a):
    wo = _wo(tenant_a)
    at = timezone.now() - datetime.timedelta(hours=3)
    body = {"to_status": "in_progress", "client_uuid": str(uuid.uuid4()), "at": _iso(at)}
    client = api_client(tenant_a)
    assert client.post(f"/api/v1/work-orders/{wo.pk}/transition/", body, format="json").status_code == 200
    assert client.post(f"/api/v1/work-orders/{wo.pk}/transition/", body, format="json").status_code == 200
    assert _get(tenant_a, WorkOrder, pk=wo.pk).started_at == at


# --- cierre ---------------------------------------------------------------------------------


def test_complete_uses_the_phone_time_and_is_idempotent(tenant_a):
    tenant_a.org.require_close_approval = True
    tenant_a.org.save(update_fields=["require_close_approval"])
    wo = _wo(tenant_a, work_type="improvement")
    client = field_client(tenant_a.tech)
    started = timezone.now() - datetime.timedelta(hours=3)
    done = timezone.now() - datetime.timedelta(hours=1)
    client.post(_url(wo, "transition"), {"to_status": "in_progress", "at": _iso(started)}, format="json")
    body = {"labor_minutes": 120, "notes": "Listo", "client_uuid": str(uuid.uuid4()), "at": _iso(done)}
    response = client.post(_url(wo, "complete"), body, format="json")
    assert response.status_code == 200 and response.json()["status"] == "completed"
    wo = _get(tenant_a, WorkOrder, pk=wo.pk)
    assert wo.completed_at == done and wo.started_at == started
    assert client.post(_url(wo, "complete"), body, format="json").status_code == 200
    with tenant_context(tenant_a.org):
        assert LaborEntry.objects.filter(work_order=wo).count() == 1


def test_field_does_not_reopen_a_work_order_completed_by_someone_else(tenant_a):
    tenant_a.org.require_close_approval = True
    tenant_a.org.save(update_fields=["require_close_approval"])
    wo = _wo(tenant_a, work_type="improvement")
    _set_status(tenant_a, wo, S.COMPLETED, completed_at=timezone.now())
    client = field_client(tenant_a.tech)
    for suffix, body in (("transition", {"to_status": "in_progress"}), ("complete", {"labor_minutes": 10})):
        response = client.post(_url(wo, suffix), body, format="json")
        assert response.status_code == 409, suffix
        assert response.json()["error"]["code"] == "locked"
    with tenant_context(tenant_a.org):
        assert WorkOrder.objects.get(pk=wo.pk).status == S.COMPLETED
        assert not LaborEntry.objects.exists()


def test_complete_on_a_cancelled_work_order_is_a_409(tenant_a):
    wo = _wo(tenant_a, work_type="improvement")
    _set_status(tenant_a, wo, S.CANCELLED)
    response = field_client(tenant_a.tech).post(_url(wo, "complete"), {"labor_minutes": 10}, format="json")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "cancelled"


# --- checklist ------------------------------------------------------------------------------


def test_answer_last_one_wins_by_answered_at(tenant_a):
    wo = _wo(tenant_a)
    item = _item(tenant_a, wo)
    client = field_client(tenant_a.tech)
    url = f"{_url(wo, 'checklist-items')}{item.pk}/answer/"
    late = timezone.now() - datetime.timedelta(hours=1)
    early = timezone.now() - datetime.timedelta(hours=2)
    first = client.post(url, {"result": "not_ok", "answered_at": _iso(late)}, format="json")
    assert first.status_code == 200
    assert first.json()["superseded"] is False
    assert datetime.datetime.fromisoformat(first.json()["answered_at"]) == late
    older = client.post(url, {"result": "ok", "answered_at": _iso(early)}, format="json")
    assert older.status_code == 200
    assert older.json()["superseded"] is True and older.json()["result"] == "not_ok"
    assert _get(tenant_a, WorkOrderChecklistItem, pk=item.pk).result == "not_ok"


def test_answer_on_a_cancelled_work_order_is_a_409(tenant_a):
    wo = _wo(tenant_a)
    item = _item(tenant_a, wo)
    _set_status(tenant_a, wo, S.CANCELLED)
    url = f"{_url(wo, 'checklist-items')}{item.pk}/answer/"
    response = field_client(tenant_a.tech).post(url, {"result": "ok"}, format="json")
    assert response.status_code == 409 and response.json()["error"]["code"] == "cancelled"


def test_checklist_items_are_listed(tenant_a):
    wo = _wo(tenant_a)
    _item(tenant_a, wo)
    response = field_client(tenant_a.tech).get(_url(wo, "checklist-items"))
    assert response.status_code == 200
    assert response.json()["count"] == 1


# --- fotos ----------------------------------------------------------------------------------


def test_photo_taken_before_completion_is_accepted_after_it(tenant_a):
    wo = _wo(tenant_a)
    completed = timezone.now() - datetime.timedelta(hours=1)
    _set_status(tenant_a, wo, S.COMPLETED, completed_at=completed)
    client = field_client(tenant_a.tech)
    before = {"file": _jpeg(), "taken_at": _iso(completed - datetime.timedelta(minutes=5))}
    response = client.post(_url(wo, "photos"), before, format="multipart")
    assert response.status_code == 201
    after = {"file": _jpeg(), "taken_at": _iso(completed + datetime.timedelta(minutes=5))}
    response = client.post(_url(wo, "photos"), after, format="multipart")
    assert response.status_code == 409 and response.json()["error"]["code"] == "locked"


def test_item_photo_with_taken_at_answers_the_item_and_is_idempotent(tenant_a):
    wo = _wo(tenant_a)
    item = _item(tenant_a, wo, item_type="photo")
    client = field_client(tenant_a.tech)
    taken = timezone.now() - datetime.timedelta(hours=2)
    key = str(uuid.uuid4())
    url = f"{_url(wo, 'checklist-items')}{item.pk}/photos/"
    body = lambda: {"file": _jpeg(), "taken_at": _iso(taken), "client_uuid": key}  # noqa: E731
    assert client.post(url, body(), format="multipart").status_code == 201
    assert client.post(url, body(), format="multipart").status_code == 200
    item = _get(tenant_a, WorkOrderChecklistItem, pk=item.pk)
    assert item.result == "done" and item.answered_at == taken
    with tenant_context(tenant_a.org):
        assert (
            Attachment.objects.filter(target_type=TargetType.CHECKLIST_ITEM, target_id=item.pk).count() == 1
        )


# --- horas ----------------------------------------------------------------------------------


def test_labor_is_idempotent_by_client_uuid(tenant_a):
    wo, other = _wo(tenant_a), _wo(tenant_a)
    client = field_client(tenant_a.tech)
    key = str(uuid.uuid4())
    first = client.post(_url(wo, "labor"), {"minutes": 45, "client_uuid": key}, format="json")
    assert first.status_code == 201
    assert first.json()["minutes"] == 45 and first.json()["client_uuid"] == key
    replay = client.post(_url(wo, "labor"), {"minutes": 45, "client_uuid": key}, format="json")
    assert replay.status_code == 200 and replay.json()["id"] == first.json()["id"]
    elsewhere = client.post(_url(other, "labor"), {"minutes": 45, "client_uuid": key}, format="json")
    assert elsewhere.status_code == 409 and elsewhere.json()["error"]["code"] == "conflict"
    with tenant_context(tenant_a.org):
        assert LaborEntry.objects.count() == 1


def test_labor_validation_and_closed_work_order(tenant_a):
    wo = _wo(tenant_a)
    client = field_client(tenant_a.tech)
    assert client.post(_url(wo, "labor"), {"minutes": 0}, format="json").status_code == 400
    _set_status(tenant_a, wo, S.CLOSED)
    response = client.post(_url(wo, "labor"), {"minutes": 30}, format="json")
    assert response.status_code == 409 and response.json()["error"]["code"] == "locked"


# --- solicitudes, lecturas y medidores ------------------------------------------------------


def test_request_with_photo_and_reported_at(tenant_a):
    client = field_client(tenant_a.tech)
    reported = timezone.now() - datetime.timedelta(hours=4)
    key = str(uuid.uuid4())
    body = lambda: {  # noqa: E731
        "description": "Ruido en el compresor",
        "asset": str(tenant_a.asset.pk),
        "asset_stopped": "true",
        "photo": _jpeg(),
        "reported_at": _iso(reported),
        "client_uuid": key,
    }
    response = client.post(f"{FIELD_API}/requests/", body(), format="multipart")
    assert response.status_code == 201, response.json()
    data = response.json()
    assert data["number"] and data["work_order_code"] and data["photo_warning"] == ""
    assert client.post(f"{FIELD_API}/requests/", body(), format="multipart").json()["id"] == data["id"]
    with tenant_context(tenant_a.org):
        request = WorkRequest.objects.get()
        assert (
            Attachment.objects.filter(
                target_type=TargetType.WORK_ORDER, target_id=request.work_order_id
            ).count()
            == 1
        )
        assert request.work_order.failure.started_at == reported


def test_request_as_json_without_photo(tenant_a):
    response = field_client(tenant_a.viewer).post(
        f"{FIELD_API}/requests/", {"description": "Gotea", "asset": str(tenant_a.asset.pk)}, format="json"
    )
    assert response.status_code == 201


def test_reading_is_recorded_as_manual_and_meters_are_listed(tenant_a):
    with tenant_context(tenant_a.org):
        meter = Meter.objects.create(asset=tenant_a.asset, kind=MeterKind.HOURS)
    client = field_client(tenant_a.tech)
    meters = client.get(f"{FIELD_API}/meters/")
    assert meters.status_code == 200 and meters.json()["results"][0]["id"] == str(meter.pk)
    key = str(uuid.uuid4())
    body = {"meter": str(meter.pk), "value": "120.5", "client_uuid": key}
    response = client.post(f"{FIELD_API}/readings/", body, format="json")
    assert response.status_code == 201 and response.json()["source"] == "manual"
    assert client.post(f"{FIELD_API}/readings/", body, format="json").json()["id"] == response.json()["id"]
    with tenant_context(tenant_a.org):
        assert MeterReading.objects.filter(meter=meter).count() == 1


# --- versión del cliente, CSRF y límite -----------------------------------------------------


def test_old_client_must_upgrade(tenant_a):
    wo = _wo(tenant_a)
    client = field_client(tenant_a.tech)
    headers = {"HTTP_X_CLIENT_VERSION": str(MIN_CLIENT_VERSION - 1)}
    response = client.post(_url(wo, "transition"), {"to_status": "in_progress"}, format="json", **headers)
    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "upgrade_required"
    assert error["details"] == {"min_client_version": MIN_CLIENT_VERSION}
    assert _get(tenant_a, WorkOrder, pk=wo.pk).status == S.PLANNED
    # bootstrap responde igual: así el cliente viejo sabe qué versión necesita.
    assert client.get(f"{FIELD_API}/bootstrap/", **headers).status_code == 200
    current = {"HTTP_X_CLIENT_VERSION": str(MIN_CLIENT_VERSION)}
    assert client.get(f"{FIELD_API}/snapshot/", **current).status_code == 200


def test_writes_require_the_csrf_token(tenant_a):
    wo = _wo(tenant_a)
    client = field_client(tenant_a.tech, enforce_csrf_checks=True)
    response = client.post(_url(wo, "transition"), {"to_status": "in_progress"}, format="json")
    assert response.status_code == 403
    client.get("/field/")  # el shell deja la cookie csrftoken
    token = client.cookies["csrftoken"].value
    response = client.post(
        _url(wo, "transition"), {"to_status": "in_progress"}, format="json", HTTP_X_CSRFTOKEN=token
    )
    assert response.status_code == 200
    assert client.get(f"{FIELD_API}/snapshot/").status_code == 200  # leer no pide el token


def test_rate_limit_is_per_person(tenant_a):
    rates = {**api_settings.DEFAULT_THROTTLE_RATES, "field_user": "2/min"}
    SimpleRateThrottle.THROTTLE_RATES = rates
    try:
        from django.core.cache import cache

        cache.clear()
        client = field_client(tenant_a.tech)
        codes = [client.get(f"{FIELD_API}/bootstrap/").status_code for _ in range(3)]
        assert codes == [200, 200, 429]
        assert field_client(tenant_a.admin).get(f"{FIELD_API}/bootstrap/").status_code == 200
    finally:
        SimpleRateThrottle.THROTTLE_RATES = api_settings.DEFAULT_THROTTLE_RATES


# --- repuestos sin conexión (INV-13) --------------------------------------------------------


@pytest.fixture
def parts(tenant_a, reconciled_inventory):
    """Catálogo con un repuesto con stock a $1.000, uno sin stock y uno desactivado (y, al final
    de cada test, el saldo cuadra con el libro)."""
    with tenant_context(tenant_a.org):
        bearing = inventory.create_part(membership=tenant_a.admin, code="ROD-6204", name="Rodamiento")
        grease = inventory.create_part(
            membership=tenant_a.admin, code="GRA-01", name="Grasa", unit="kg", category="Lubricantes"
        )
        old = inventory.create_part(membership=tenant_a.admin, code="OLD-01", name="Descontinuado")
        inventory.set_part_active(membership=tenant_a.admin, part=old, is_active=False)
        inventory.receive_stock(membership=tenant_a.admin, part=bearing, quantity=5, unit_cost=1000)
    return bearing, grease


def _parts_plan(tenant, features):
    tenant.org.plan = Plan.objects.create(code="p-" + "-".join(features), name="Plan", features=features)
    tenant.org.save(update_fields=["plan"])


def test_reference_brings_the_parts_catalog_without_costs(tenant_a, parts, monkeypatch):
    data = field_client(tenant_a.tech).get(f"{FIELD_API}/reference/").json()
    assert [p["code"] for p in data["parts"]] == ["GRA-01", "ROD-6204"]  # sin desactivados
    assert set(data["parts"][0]) == {"id", "code", "name", "unit", "category"}
    assert data["parts"][0]["unit"] == "kg" and data["parts_truncated"] is False
    monkeypatch.setattr("apps.inventory.selectors.FIELD_PART_LIMIT", 1)
    data = field_client(tenant_a.tech).get(f"{FIELD_API}/reference/").json()
    assert len(data["parts"]) == 1 and data["parts_truncated"] is True


def test_reference_has_no_parts_without_the_inventory_module(tenant_a, parts):
    _parts_plan(tenant_a, ["assets", "work_orders", "requests"])
    data = field_client(tenant_a.tech).get(f"{FIELD_API}/reference/").json()
    assert data["parts"] == [] and data["parts_truncated"] is False


def test_snapshot_brings_planned_and_used_parts(tenant_a, parts):
    bearing, grease = parts
    wo = _wo(tenant_a, assigned_to=tenant_a.tech.user)
    with tenant_context(tenant_a.org):
        inventory.reserve_for_work_order(work_order=wo, part=bearing, quantity=2, source="manual")
        inventory.reserve_for_work_order(work_order=wo, part=grease, quantity="0.5", source="manual")
        add_part_usage(work_order=wo, membership=tenant_a.tech, part=grease, quantity="0.5")
        add_part_usage(work_order=wo, membership=tenant_a.tech, description="Huaipe", quantity=1, unit_cost=0)
    (row,) = field_client(tenant_a.tech).get(f"{FIELD_API}/snapshot/").json()["work_orders"]
    # La grasa ya se usó entera: solo queda prevista la que tiene pendiente.
    (planned,) = row["planned_parts"]
    assert set(planned) == PLANNED_PART_KEYS
    assert planned["part"] == str(bearing.pk) and planned["code"] == "ROD-6204"
    assert (planned["quantity"], planned["consumed_quantity"], planned["pending"]) == (
        "2.000",
        "0.000",
        "2.000",
    )
    assert [p["description"] for p in row["parts"]] == ["GRA-01 · Grasa", "Huaipe"]
    assert set(row["parts"][0]) == PART_USAGE_KEYS and row["parts"][1]["part"] is None


def test_snapshot_without_the_inventory_module_has_no_planned_parts(tenant_a, parts):
    bearing, _grease = parts
    wo = _wo(tenant_a)
    with tenant_context(tenant_a.org):
        inventory.reserve_for_work_order(work_order=wo, part=bearing, quantity=1, source="manual")
    _parts_plan(tenant_a, ["assets", "work_orders", "requests"])
    (row,) = field_client(tenant_a.tech).get(f"{FIELD_API}/snapshot/").json()["work_orders"]
    assert row["planned_parts"] == []


def test_offline_part_usage_consumes_the_reservation_once(tenant_a, parts):
    bearing, _grease = parts
    wo = _wo(tenant_a, assigned_to=tenant_a.tech.user)
    with tenant_context(tenant_a.org):
        inventory.reserve_for_work_order(work_order=wo, part=bearing, quantity=2, source="manual")
    client = field_client(tenant_a.tech)
    used_at = timezone.now() - datetime.timedelta(hours=2)
    body = {
        "part": str(bearing.pk),
        "quantity": "2",
        "client_uuid": str(uuid.uuid4()),
        "used_at": _iso(used_at),
    }
    response = client.post(_url(wo, "parts"), body, format="json")
    assert response.status_code == 201
    usage = response.json()
    assert usage["description"] == "ROD-6204 · Rodamiento"
    assert usage["unit_cost"] is None and usage["cost"] is None  # el técnico no ve costos (Q3)
    again = client.post(_url(wo, "parts"), body, format="json")
    assert again.status_code == 200 and again.json()["id"] == usage["id"]
    with tenant_context(tenant_a.org):
        assert PartUsage.objects.filter(work_order=wo).count() == 1
        movement = StockMovement.objects.get(work_order=wo)
        assert movement.occurred_at == used_at.replace(microsecond=movement.occurred_at.microsecond)
        assert StockItem.objects.get(part=bearing).on_hand == 3
        assert Reservation.objects.get(work_order=wo).status == Reservation.Status.FULFILLED
        assert WorkOrder.objects.get(pk=wo.pk).total_cost == Decimal("2000.00")
    # El jefe sí ve el costo de la línea.
    listed = field_client(tenant_a.admin).get(_url(wo, "parts")).json()["results"]
    assert listed[0]["unit_cost"] == "1000.0000"


def test_offline_part_usage_never_conflicts_on_stock(tenant_a, parts):
    _bearing, grease = parts  # sin stock
    wo = _wo(tenant_a)
    body = {"part": str(grease.pk), "quantity": "3", "client_uuid": str(uuid.uuid4())}
    assert field_client(tenant_a.tech).post(_url(wo, "parts"), body, format="json").status_code == 201
    with tenant_context(tenant_a.org):
        assert StockItem.objects.get(part=grease).on_hand == -3


def test_offline_free_text_part_without_cost(tenant_a):
    wo = _wo(tenant_a)
    body = {"description": "Huaipe", "quantity": "2", "unit": "kg", "client_uuid": str(uuid.uuid4())}
    response = field_client(tenant_a.tech).post(_url(wo, "parts"), body, format="json")
    assert response.status_code == 201
    with tenant_context(tenant_a.org):
        usage = PartUsage.objects.get(work_order=wo)
        assert usage.part_id is None and usage.unit_cost == 0 and usage.unit == "kg"
    missing = field_client(tenant_a.tech).post(_url(wo, "parts"), {"quantity": "1"}, format="json")
    assert missing.status_code == 400 and "description" in missing.json()["error"]["details"]


@pytest.mark.parametrize(("status", "code"), [(S.CANCELLED, "cancelled"), (S.CLOSED, "locked")])
def test_offline_part_usage_on_a_finished_work_order_is_a_409(tenant_a, parts, status, code):
    bearing, _grease = parts
    wo = _wo(tenant_a)
    _set_status(tenant_a, wo, status)
    body = {"part": str(bearing.pk), "quantity": "1", "client_uuid": str(uuid.uuid4())}
    response = field_client(tenant_a.tech).post(_url(wo, "parts"), body, format="json")
    assert response.status_code == 409 and response.json()["error"]["code"] == code
    with tenant_context(tenant_a.org):
        assert not StockMovement.objects.filter(kind="issue").exists()


def test_offline_part_usage_can_be_sent_after_completing(tenant_a, parts):
    """Una OT completada (sin cerrar) aún admite costos, como en la web."""
    bearing, _grease = parts
    wo = _wo(tenant_a)
    _set_status(tenant_a, wo, S.COMPLETED)
    body = {"part": str(bearing.pk), "quantity": "1", "client_uuid": str(uuid.uuid4())}
    assert field_client(tenant_a.tech).post(_url(wo, "parts"), body, format="json").status_code == 201


def test_offline_part_usage_key_resent_after_close_and_used_elsewhere(tenant_a, parts):
    bearing, _grease = parts
    first, second = _wo(tenant_a), _wo(tenant_a)
    client = field_client(tenant_a.tech)
    body = {"part": str(bearing.pk), "quantity": "1", "client_uuid": str(uuid.uuid4())}
    created = client.post(_url(first, "parts"), body, format="json").json()
    _set_status(tenant_a, first, S.CLOSED)
    again = client.post(_url(first, "parts"), body, format="json")
    assert again.status_code == 200 and again.json()["id"] == created["id"]
    other = client.post(_url(second, "parts"), body, format="json")
    assert other.status_code == 409 and other.json()["error"]["code"] == "conflict"


def test_offline_catalog_part_needs_the_inventory_module(tenant_a, parts):
    bearing, _grease = parts
    wo = _wo(tenant_a)
    _parts_plan(tenant_a, ["assets", "work_orders", "requests"])
    client = field_client(tenant_a.tech)
    response = client.post(_url(wo, "parts"), {"part": str(bearing.pk), "quantity": "1"}, format="json")
    assert response.status_code == 403
    assert "Inventario y repuestos" in response.json()["error"]["message"]
    free = client.post(_url(wo, "parts"), {"description": "Huaipe", "quantity": "1"}, format="json")
    assert free.status_code == 201
