"""Aislamiento de la API v1 de condición (CBM-08 CA3, CBM-09): la organización B no ve, edita,
reconoce ni referencia reglas, alertas, variables ni clases de A."""

import pytest

from apps.assets.models import Asset, MeterKind, MeterReading
from apps.condition import services
from apps.condition.models import ConditionAlert, ConditionRule
from apps.core.tenancy import tenant_context
from tests.api_utils import api_client
from tests.condition_utils import read, variable

RULES = "/api/v1/condition-rules/"
ALERTS = "/api/v1/condition-alerts/"


@pytest.fixture
def episode_a(tenant_a):
    """En A: una variable en un activo con código propio, una regla y una alerta abierta."""
    with tenant_context(tenant_a.org):
        asset = Asset.objects.create(code="SOLO-A", name="Solo A", asset_class=tenant_a.asset.asset_class)
        meter = variable(asset)
        rule = services.create_rule(membership=tenant_a.admin, meter=meter, operator="gt", threshold="80")
        read(meter, "85")
        alert = ConditionAlert.objects.get()
    return {"asset": asset, "meter": meter, "rule": rule, "alert": alert}


def test_lists_and_details_only_show_own_rows(tenant_a, tenant_b, episode_a):
    client = api_client(tenant_b)
    assert client.get(RULES, {"include_inactive": "true"}).json()["count"] == 0
    assert client.get(ALERTS).json()["count"] == 0
    assert client.get(f"{RULES}{episode_a['rule'].pk}/").status_code == 404
    assert client.get(f"{ALERTS}{episode_a['alert'].pk}/").status_code == 404
    # Filtros con ids de A: listas vacías, sin fuga.
    assert client.get(ALERTS, {"asset": str(episode_a["asset"].pk)}).json()["count"] == 0
    assert client.get(RULES, {"meter": str(episode_a["meter"].pk)}).json()["count"] == 0
    assert client.get(RULES, {"asset_class": str(tenant_a.asset.asset_class_id)}).json()["count"] == 0
    assert api_client(tenant_a).get(ALERTS).json()["count"] == 1


def test_cannot_edit_or_acknowledge_rows_of_another_organization(tenant_a, tenant_b, episode_a):
    client = api_client(tenant_b)
    rule_url = f"{RULES}{episode_a['rule'].pk}/"
    assert client.patch(rule_url, {"threshold": "1"}, format="json").status_code == 404
    assert client.patch(rule_url, {"is_active": False}, format="json").status_code == 404
    assert client.post(f"{ALERTS}{episode_a['alert'].pk}/acknowledge/").status_code == 404
    with tenant_context(tenant_a.org):
        rule = ConditionRule.objects.get()
        assert rule.threshold == 80 and rule.is_active
        assert ConditionAlert.objects.get().status == ConditionAlert.Status.OPEN


def test_cannot_reference_meters_or_classes_of_another_organization(tenant_a, tenant_b, episode_a):
    client = api_client(tenant_b)
    by_meter = {"meter": str(episode_a["meter"].pk), "operator": "gt", "threshold": "1"}
    response = client.post(RULES, by_meter, format="json")
    assert response.status_code == 400 and "meter" in response.json()["error"]["details"]
    by_class = {
        "asset_class": str(tenant_a.asset.asset_class_id),
        "meter_kind": MeterKind.TEMPERATURE,
        "operator": "gt",
        "threshold": "1",
    }
    response = client.post(RULES, by_class, format="json")
    assert response.status_code == 400 and "asset_class" in response.json()["error"]["details"]
    with tenant_context(tenant_b.org):
        assert not ConditionRule.objects.exists()


def test_bulk_readings_cannot_reach_variables_of_another_organization(tenant_a, tenant_b, episode_a):
    rows = [
        {"meter": str(episode_a["meter"].pk), "value": "1", "read_at": "2026-10-02"},
        {
            "asset_code": "SOLO-A",
            "kind": "temperature",
            "name": "Rodamiento",
            "value": "1",
            "read_at": "2026-10-02",
        },
    ]
    body = api_client(tenant_b).post("/api/v1/readings/bulk/", {"rows": rows}, format="json").json()
    assert (body["created"], body["errors"]) == (0, 2)
    single = {"meter": str(episode_a["meter"].pk), "value": "-1"}
    assert api_client(tenant_b).post("/api/v1/readings/", single, format="json").status_code == 400
    with tenant_context(tenant_a.org):
        assert MeterReading.objects.count() == 1
