"""Permisos de la API v1 de condición (CBM-08, CBM-09): alcance del token, rol y módulo del plan."""

import pytest

from apps.accounts.models import Membership
from apps.assets.models import Meter, MeterKind
from apps.condition import services
from apps.condition.models import ConditionAlert, ConditionRule
from apps.core.tenancy import tenant_context
from apps.organizations.models import Plan
from apps.organizations.plans import BASE_MODULES
from tests.api_utils import api_client
from tests.condition_utils import read, variable

RULES = "/api/v1/condition-rules/"
ALERTS = "/api/v1/condition-alerts/"


@pytest.fixture
def alert(tenant_a):
    with tenant_context(tenant_a.org):
        meter = variable(tenant_a.asset)
        services.create_rule(membership=tenant_a.admin, meter=meter, operator="gt", threshold="80")
        read(meter, "85")
        return ConditionAlert.objects.select_related("meter").get()


def _new_rule(alert):
    return {"meter": str(alert.meter_id), "operator": "lt", "threshold": "1"}


def test_read_scope_token_reads_but_cannot_write(tenant_a, alert):
    client = api_client(tenant_a, scope="read")
    assert client.get(RULES).status_code == 200
    assert client.get(ALERTS).status_code == 200
    assert client.get(f"{ALERTS}{alert.pk}/").status_code == 200
    assert client.post(RULES, _new_rule(alert), format="json").status_code == 403
    response = client.patch(f"{RULES}{alert.rule_id}/", {"is_active": False}, format="json")
    assert response.status_code == 403
    assert client.post(f"{ALERTS}{alert.pk}/acknowledge/").status_code == 403
    with tenant_context(tenant_a.org):
        assert ConditionRule.objects.filter(is_active=True).count() == 1
        assert ConditionAlert.objects.get().status == ConditionAlert.Status.OPEN


def test_token_acts_as_planner_and_manages(tenant_a, alert):
    client = api_client(tenant_a)
    assert client.post(RULES, _new_rule(alert), format="json").status_code == 201
    assert client.post(f"{ALERTS}{alert.pk}/acknowledge/").status_code == 200


@pytest.mark.parametrize(
    ("role", "view", "manage"),
    [
        (Membership.Role.SUPERVISOR, True, False),
        (Membership.Role.VIEWER, True, False),
        (Membership.Role.EXTERNAL, False, False),
    ],
)
def test_endpoints_follow_condition_permissions_of_the_principal(
    monkeypatch, tenant_a, alert, role, view, manage
):
    """Si el rol del token cambiara, los endpoints siguen `condition.view` y `condition.manage`."""
    monkeypatch.setattr("apps.api.services.SERVICE_ROLE", role)
    client = api_client(tenant_a)
    assert client.get(RULES).status_code == (200 if view else 403)
    assert client.get(ALERTS).status_code == (200 if view else 403)
    assert client.post(RULES, _new_rule(alert), format="json").status_code == (201 if manage else 403)
    assert client.post(f"{ALERTS}{alert.pk}/acknowledge/").status_code == (200 if manage else 403)


def test_without_condition_module_endpoints_are_forbidden_but_readings_work(tenant_a, alert):
    tenant_a.org.plan = Plan.objects.create(
        code="sin-condicion", name="Plan", features=[*BASE_MODULES, "api"]
    )
    tenant_a.org.save(update_fields=["plan"])
    client = api_client(tenant_a)
    for response in (
        client.get(RULES),
        client.get(f"{RULES}{alert.rule_id}/"),
        client.post(RULES, _new_rule(alert), format="json"),
        client.get(ALERTS),
        client.post(f"{ALERTS}{alert.pk}/acknowledge/"),
    ):
        assert response.status_code == 403
        assert "no está incluido en tu plan" in response.json()["error"]["message"]
    # Las variables siguen siendo medidores: sus lecturas se registran (sin evaluar reglas).
    reading = {"meter": str(alert.meter_id), "value": "-5"}
    assert client.post("/api/v1/readings/", reading, format="json").status_code == 201
    row = {"asset_code": "CMP-01", "kind": "temperature", "value": "90", "read_at": "2026-10-03"}
    assert client.post("/api/v1/readings/bulk/", {"rows": [row]}, format="json").json()["created"] == 1
    with tenant_context(tenant_a.org):
        assert ConditionAlert.objects.count() == 1
        assert Meter.objects.get(kind=MeterKind.TEMPERATURE).current_value is not None
