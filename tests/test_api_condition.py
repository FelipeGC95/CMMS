"""API v1 de mantenimiento por condición (CBM-09) y lecturas de variables por API (CBM-02 CA3)."""

import uuid
from decimal import Decimal

import pytest

from apps.assets.models import Meter, MeterKind, MeterReading
from apps.assets.services import set_variable_active
from apps.condition.models import ConditionAlert, ConditionRule
from apps.core.tenancy import tenant_context
from apps.work.models import WorkOrder
from apps.work.services import create_work_order
from tests.api_utils import api_client
from tests.condition_utils import read, variable

pytestmark = pytest.mark.django_db

RULES = "/api/v1/condition-rules/"
ALERTS = "/api/v1/condition-alerts/"
BULK = "/api/v1/readings/bulk/"

RULE_KEYS = {
    "id",
    "name",
    "label",
    "meter",
    "meter_asset_code",
    "asset_class",
    "asset_class_code",
    "meter_kind",
    "meter_name",
    "operator",
    "threshold",
    "min_value",
    "max_value",
    "consecutive",
    "action",
    "work_order_title",
    "work_order_priority",
    "work_order_instructions",
    "is_active",
    "created_at",
    "updated_at",
}


@pytest.fixture
def meters(tenant_a):
    """Dos variables y un horómetro en el mismo activo."""
    with tenant_context(tenant_a.org):
        return {
            "bearing": variable(tenant_a.asset, name="Rodamiento"),
            "motor": variable(tenant_a.asset, name="Motor"),
            "vibration": variable(tenant_a.asset, kind=MeterKind.VIBRATION, name="Eje"),
            "hours": Meter.objects.create(asset=tenant_a.asset, kind=MeterKind.HOURS),
        }


def _alert(tenant, meter, value="85", hours=0, action="alert_work_order"):
    from apps.condition import services

    with tenant_context(tenant.org):
        services.create_rule(
            membership=tenant.admin, meter=meter, operator="gt", threshold="80", action=action
        )
        read(meter, value, hours=hours)
        return ConditionAlert.objects.get(meter=meter)


# --- Reglas -------------------------------------------------------------------------------


def test_create_rule_on_a_variable(tenant_a, meters):
    client = api_client(tenant_a)
    body = {"meter": str(meters["bearing"].pk), "operator": "gt", "threshold": "80"}
    response = client.post(RULES, body, format="json")
    assert response.status_code == 201, response.content
    data = response.json()
    assert set(data) == RULE_KEYS
    assert data["meter_asset_code"] == "CMP-01" and data["asset_class"] is None
    assert (data["threshold"], data["consecutive"], data["action"]) == ("80.000", 1, "alert_work_order")
    assert data["is_active"] is True and data["label"]
    with tenant_context(tenant_a.org):
        rule = ConditionRule.objects.get()
        assert rule.meter_id == meters["bearing"].pk
        assert rule.created_by.api_token is not None  # atribuida al token


def test_create_class_rule_with_all_fields(tenant_a, meters):
    client = api_client(tenant_a)
    body = {
        "asset_class": str(tenant_a.asset.asset_class_id),
        "meter_kind": "vibration",
        "meter_name": "Eje",
        "operator": "outside",
        "min_value": "1",
        "max_value": "7.1",
        "consecutive": 3,
        "action": "alert",
        "name": "Vibración de compresores",
        "work_order_title": "Revisar eje",
        "work_order_priority": 2,
        "work_order_instructions": "Medir de nuevo.",
    }
    response = client.post(RULES, body, format="json")
    assert response.status_code == 201, response.content
    data = response.json()
    assert data["meter"] is None and data["asset_class_code"] == "CMP"
    assert (data["meter_kind"], data["meter_name"], data["threshold"]) == ("vibration", "Eje", None)
    assert (data["min_value"], data["max_value"], data["work_order_priority"]) == ("1.000", "7.100", 2)
    assert data["label"] == "Vibración de compresores"


@pytest.mark.parametrize(
    ("body", "field"),
    [
        ({"operator": "gt", "threshold": "1"}, "non_field_errors"),  # sin alcance
        ({"operator": "outside", "min_value": "8", "max_value": "2"}, "max_value"),
        ({"operator": "outside", "min_value": "2"}, "min_value"),
        ({"operator": "rate", "threshold": "0"}, "threshold"),
        ({"operator": "gt"}, "threshold"),
        ({"operator": "nope", "threshold": "1"}, "operator"),
        ({"operator": "gt", "threshold": "1", "consecutive": 0}, "consecutive"),
        ({"operator": "gt", "threshold": "1", "work_order_priority": 9}, "work_order_priority"),
    ],
)
def test_create_rule_validations(tenant_a, meters, body, field):
    client = api_client(tenant_a)
    if field != "non_field_errors":
        body = {**body, "meter": str(meters["bearing"].pk)}
    response = client.post(RULES, body, format="json")
    assert response.status_code == 400, response.content
    error = response.json()["error"]
    assert error["code"] == "invalid" and field in error["details"]
    with tenant_context(tenant_a.org):
        assert not ConditionRule.objects.exists()


def test_create_rule_scope_must_be_one_variable_or_one_class(tenant_a, meters):
    client = api_client(tenant_a)
    both = {
        "meter": str(meters["bearing"].pk),
        "asset_class": str(tenant_a.asset.asset_class_id),
        "meter_kind": "temperature",
        "operator": "gt",
        "threshold": "1",
    }
    assert client.post(RULES, both, format="json").status_code == 400
    usage = {"meter": str(meters["hours"].pk), "operator": "gt", "threshold": "1"}
    response = client.post(RULES, usage, format="json")
    assert response.status_code == 400 and "meter" in response.json()["error"]["details"]
    usage_kind = {"asset_class": str(tenant_a.asset.asset_class_id), "meter_kind": "hours", "operator": "gt"}
    response = client.post(RULES, {**usage_kind, "threshold": "1"}, format="json")
    assert response.status_code == 400 and "meter_kind" in response.json()["error"]["details"]
    unknown = {"meter": str(uuid.uuid4()), "operator": "gt", "threshold": "1"}
    response = client.post(RULES, unknown, format="json")
    assert response.status_code == 400 and "meter" in response.json()["error"]["details"]


def test_create_rule_is_idempotent_by_client_uuid(tenant_a, meters):
    client = api_client(tenant_a)
    key = str(uuid.uuid4())
    body = {"meter": str(meters["bearing"].pk), "operator": "gt", "threshold": "80", "client_uuid": key}
    first = client.post(RULES, body, format="json")
    again = client.post(RULES, {**body, "threshold": "90"}, format="json")
    assert (first.status_code, again.status_code) == (201, 200)
    assert again.json()["id"] == first.json()["id"] and again.json()["threshold"] == "80.000"
    with tenant_context(tenant_a.org):
        assert ConditionRule.objects.count() == 1
        wo = create_work_order(work_type=WorkOrder.Type.CORRECTIVE, title="x", asset=tenant_a.asset)
    # La misma clave en otra operación (cambiar el estado de una OT) es un conflicto.
    transition = {"to_status": "in_progress", "client_uuid": key}
    response = client.post(f"/api/v1/work-orders/{wo.pk}/transition/", transition, format="json")
    assert response.status_code == 409
    other = {"meter": str(meters["motor"].pk), "operator": "gt", "threshold": "1", "client_uuid": key}
    assert client.post(RULES, other, format="json").json()["meter"] == str(meters["bearing"].pk)


def test_rule_list_filters_and_detail(tenant_a, meters):
    from apps.condition import services

    with tenant_context(tenant_a.org):
        direct = services.create_rule(
            membership=tenant_a.admin, meter=meters["bearing"], operator="gt", threshold="80"
        )
        by_class = services.create_rule(
            membership=tenant_a.admin,
            asset_class=tenant_a.asset.asset_class,
            meter_kind=MeterKind.VIBRATION,
            meter_name="Eje",
            operator="gt",
            threshold="7",
        )
        off = services.create_rule(
            membership=tenant_a.admin, meter=meters["motor"], operator="lt", threshold="1"
        )
        services.set_rule_active(membership=tenant_a.admin, rule=off, is_active=False)
    client = api_client(tenant_a)
    ids = lambda response: {r["id"] for r in response.json()["results"]}  # noqa: E731
    assert ids(client.get(RULES)) == {str(direct.pk), str(by_class.pk)}
    assert ids(client.get(RULES, {"include_inactive": "true"})) == {
        str(direct.pk),
        str(by_class.pk),
        str(off.pk),
    }
    assert ids(client.get(RULES, {"meter": str(meters["vibration"].pk)})) == {str(by_class.pk)}
    assert ids(client.get(RULES, {"asset_class": str(tenant_a.asset.asset_class_id)})) == {str(by_class.pk)}
    assert ids(client.get(RULES, {"meter": str(uuid.uuid4())})) == set()
    assert client.get(RULES, {"meter": "x"}).status_code == 400
    detail = client.get(f"{RULES}{off.pk}/")
    assert detail.status_code == 200 and detail.json()["is_active"] is False


def test_patch_rule_changes_condition_and_deactivates(tenant_a, meters):
    alert = _alert(tenant_a, meters["bearing"])
    client = api_client(tenant_a)
    url = f"{RULES}{alert.rule_id}/"
    response = client.patch(url, {"threshold": "90", "work_order_priority": 1}, format="json")
    assert response.status_code == 200, response.content
    assert (response.json()["threshold"], response.json()["work_order_priority"]) == ("90.000", 1)
    invalid = client.patch(url, {"operator": "outside", "min_value": "5", "max_value": "1"}, format="json")
    assert invalid.status_code == 400 and "max_value" in invalid.json()["error"]["details"]
    scope = client.patch(url, {"meter": str(meters["motor"].pk)}, format="json")
    assert scope.status_code == 400 and "meter" in scope.json()["error"]["details"]
    off = client.patch(url, {"is_active": False}, format="json")
    assert off.status_code == 200 and off.json()["is_active"] is False
    with tenant_context(tenant_a.org):
        alert.refresh_from_db()
        assert alert.status == ConditionAlert.Status.NORMALIZED
        assert ConditionRule.objects.get().threshold == Decimal("90")
    assert client.patch(url, {"is_active": True}, format="json").json()["is_active"] is True


def test_rules_cannot_be_deleted_or_replaced(tenant_a, meters):
    alert = _alert(tenant_a, meters["bearing"])
    client = api_client(tenant_a)
    assert client.delete(f"{RULES}{alert.rule_id}/").status_code == 405
    assert client.put(f"{RULES}{alert.rule_id}/", {}, format="json").status_code == 405
    assert client.delete(f"{ALERTS}{alert.pk}/").status_code == 405


# --- Alertas ------------------------------------------------------------------------------


def test_alert_list_filters_and_detail(tenant_a, meters):
    from apps.assets.models import Asset

    open_alert = _alert(tenant_a, meters["bearing"])
    normalized = _alert(tenant_a, meters["motor"], action="alert")
    with tenant_context(tenant_a.org):
        read(meters["motor"], "20", hours=1)
        other = Asset.objects.create(
            code="CMP-02", name="Compresor 2", asset_class=tenant_a.asset.asset_class
        )
    client = api_client(tenant_a)
    body = client.get(ALERTS).json()
    assert [a["id"] for a in body["results"]] == [str(open_alert.pk), str(normalized.pk)]  # abiertas primero
    row = body["results"][0]
    assert (row["status"], row["value"], row["asset_code"], row["unit"]) == ("open", "85.000", "CMP-01", "°C")
    assert row["work_order_code"].startswith("OT-") and body["results"][1]["work_order"] is None
    assert client.get(ALERTS, {"status": "normalized"}).json()["count"] == 1
    assert client.get(ALERTS, {"status": "active"}).json()["count"] == 1
    assert client.get(ALERTS, {"asset": str(tenant_a.asset.pk)}).json()["count"] == 2
    assert client.get(ALERTS, {"asset": str(other.pk)}).json()["count"] == 0
    assert client.get(ALERTS, {"asset": str(uuid.uuid4())}).json()["count"] == 0
    detail = client.get(f"{ALERTS}{open_alert.pk}/")
    assert detail.status_code == 200 and detail.json()["rule"] == str(open_alert.rule_id)


def test_acknowledge_alert(tenant_a, meters):
    alert = _alert(tenant_a, meters["bearing"])
    client = api_client(tenant_a)
    url = f"{ALERTS}{alert.pk}/acknowledge/"
    response = client.post(url)
    assert response.status_code == 200, response.content
    assert response.json()["status"] == "acknowledged" and response.json()["acknowledged_at"]
    assert client.post(url).status_code == 200  # reconocerla otra vez no hace nada
    with tenant_context(tenant_a.org):
        alert.refresh_from_db()
        assert alert.acknowledged_by.api_token is not None
        read(meters["bearing"], "20", hours=1)  # vuelve a rango: normalizada
    response = client.post(url)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid"
    assert client.post(f"{ALERTS}{uuid.uuid4()}/acknowledge/").status_code == 404


# --- Lecturas: variables por tipo y nombre, negativos ----------------------------------------


def _row(**kwargs):
    return {"asset_code": "CMP-01", "read_at": "2026-10-01 08:00", **kwargs}


def test_bulk_picks_the_variable_by_kind_and_name(tenant_a, meters):
    client = api_client(tenant_a)
    rows = [
        _row(kind="temperature", name="Rodamiento", value="71,5"),
        _row(kind="temperatura", name="motor", value="-3"),  # sinónimo y sin distinguir mayúsculas
        _row(kind="vibration", value="2.5"),  # una sola variable de ese tipo: basta el tipo
        _row(kind="temperature", value="70"),  # dos temperaturas: falta el nombre
        _row(value="70"),  # varios medidores: falta tipo y nombre
        _row(kind="presion", name="x", value="1"),  # el activo no tiene esa variable
        _row(kind="nope", value="1"),
        _row(kind="hours", value="-1"),  # negativo en un medidor de uso
        {"meter": str(meters["bearing"].pk), "kind": "vibration", "value": "1", "read_at": "2026-10-01"},
    ]
    response = client.post(BULK, {"rows": rows}, format="json")
    assert response.status_code == 200, response.content
    body = response.json()
    assert (body["created"], body["errors"]) == (3, 6)
    result = {r["row"]: r for r in body["rows"]}
    assert [result[i]["result"] for i in (1, 2, 3)] == ["create"] * 3
    assert result[1]["meter"] == str(meters["bearing"].pk) and result[2]["meter"] == str(meters["motor"].pk)
    assert "nombre" in result[4]["errors"][0]
    assert "tipo y el nombre" in result[5]["errors"][0]
    assert "tipo y nombre" in result[6]["errors"][0]
    assert "desconocido" in result[7]["errors"][0]
    assert "negativa" in result[8]["errors"][0]
    assert "no coincide" in result[9]["errors"][0]
    with tenant_context(tenant_a.org):
        meters["motor"].refresh_from_db()
        assert meters["motor"].current_value == Decimal("-3")
        assert MeterReading.objects.filter(source=MeterReading.Source.API).count() == 3
    # Reenviar el lote no duplica (idempotente por medidor, fecha y valor).
    again = client.post(BULK, {"rows": rows[:3]}, format="json").json()
    assert (again["created"], again["duplicates"]) == (0, 3)


def test_bulk_reading_opens_a_condition_alert(tenant_a, meters):
    from apps.condition import services

    with tenant_context(tenant_a.org):
        services.create_rule(
            membership=tenant_a.admin, meter=meters["bearing"], operator="lt", threshold="-10"
        )
    rows = [_row(kind="temperature", name="Rodamiento", value="-12")]
    assert api_client(tenant_a).post(BULK, {"rows": rows}, format="json").json()["created"] == 1
    with tenant_context(tenant_a.org):
        assert ConditionAlert.objects.get().value == Decimal("-12")


def test_bulk_rejects_readings_of_a_deactivated_variable(tenant_a, meters):
    with tenant_context(tenant_a.org):
        set_variable_active(membership=tenant_a.admin, meter=meters["motor"], is_active=False)
    rows = [
        _row(kind="temperature", name="Motor", value="50"),
        {"meter": str(meters["motor"].pk), "value": "50", "read_at": "2026-10-01"},
    ]
    response = api_client(tenant_a).post(BULK, {"rows": rows}, format="json")
    assert response.status_code == 200
    body = response.json()
    assert (body["created"], body["errors"]) == (0, 2)
    with tenant_context(tenant_a.org):
        assert not MeterReading.objects.filter(meter=meters["motor"]).exists()


def test_single_reading_accepts_negatives_only_on_variables(tenant_a, meters):
    client = api_client(tenant_a)
    url = "/api/v1/readings/"
    response = client.post(url, {"meter": str(meters["bearing"].pk), "value": "-4.5"}, format="json")
    assert response.status_code == 201 and response.json()["value"] == "-4.500"
    response = client.post(url, {"meter": str(meters["hours"].pk), "value": "-1"}, format="json")
    assert response.status_code == 400 and "value" in response.json()["error"]["details"]
    with tenant_context(tenant_a.org):
        set_variable_active(membership=tenant_a.admin, meter=meters["motor"], is_active=False)
    response = client.post(url, {"meter": str(meters["motor"].pk), "value": "50"}, format="json")
    assert response.status_code == 400
    assert "desactivada" in str(response.json()["error"]["details"])
    with tenant_context(tenant_a.org):
        assert MeterReading.objects.count() == 1
