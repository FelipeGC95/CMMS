"""Pantallas de mantenimiento por condición (CBM-03, CBM-05): alertas, detalle y reglas."""

import pytest
from django.urls import reverse

from apps.assets.models import MeterKind
from apps.audit.models import AuditEvent
from apps.condition import services
from apps.condition.models import ConditionAlert, ConditionRule
from apps.core.tenancy import tenant_context
from apps.organizations.models import Location
from tests.condition_utils import read, variable
from tests.conftest import login


@pytest.fixture
def episode(tenant_a):
    """Una variable con regla > 80 y una alerta abierta con su OT predictiva."""
    with tenant_context(tenant_a.org):
        meter = variable(tenant_a.asset)
        rule = services.create_rule(membership=tenant_a.admin, meter=meter, operator="gt", threshold="80")
        read(meter, "85")
        read(meter, "87", hours=1)
        return meter, rule, ConditionAlert.objects.select_related("work_order").get()


# --- Alertas ----------------------------------------------------------------------


def test_alert_list_shows_active_alerts_with_value_condition_and_work_order(client, tenant_a, episode):
    _meter, _rule, alert = episode
    login(client, tenant_a.admin)
    body = client.get(reverse("condition_alert_list")).content.decode()
    assert "Abierta" in body
    assert tenant_a.asset.code in body
    assert "85 °C" in body and "87 °C" in body  # valor que la abrió y el último
    assert "&gt; 80" in body
    assert alert.work_order.code in body
    assert reverse("condition_alert_detail", args=[alert.pk]) in body
    assert reverse("condition_alert_acknowledge", args=[alert.pk]) in body


def test_alert_list_defaults_to_active_and_filters_by_status(client, tenant_a, episode):
    meter, _rule, alert = episode
    with tenant_context(tenant_a.org):
        read(meter, "70", hours=2)  # vuelve a rango: normalizada
    login(client, tenant_a.admin)
    url = reverse("condition_alert_list")
    detail = reverse("condition_alert_detail", args=[alert.pk])
    assert detail not in client.get(url).content.decode()
    assert "Sin alertas activas" in client.get(url).content.decode()
    assert detail in client.get(url, {"status": "normalized"}).content.decode()
    assert detail in client.get(url, {"status": "all"}).content.decode()
    assert detail not in client.get(url, {"status": "open"}).content.decode()


def test_alert_list_filters_by_site_and_asset(client, tenant_a, episode):
    _meter, _rule, alert = episode
    with tenant_context(tenant_a.org):
        north = Location.objects.create(code="N", name="Norte")
        south = Location.objects.create(code="S", name="Sur")
        tenant_a.asset.location = south
        tenant_a.asset.save(update_fields=["location"])
    login(client, tenant_a.admin)
    url = reverse("condition_alert_list")
    detail = reverse("condition_alert_detail", args=[alert.pk])
    assert detail in client.get(url, {"location": south.pk}).content.decode()
    assert detail not in client.get(url, {"location": north.pk}).content.decode()
    assert detail in client.get(url, {"asset": tenant_a.asset.pk}).content.decode()
    # Un valor inválido se ignora: la lista se muestra igual.
    assert client.get(url, {"asset": "no-es-uuid", "status": "x"}).status_code == 200


def test_alert_list_empty_state_invites_to_create_the_first_rule(client, tenant_a):
    login(client, tenant_a.admin)
    body = client.get(reverse("condition_alert_list")).content.decode()
    assert "Crear la primera regla" in body
    assert reverse("condition_rule_create") in body
    login(client, tenant_a.viewer)
    body = client.get(reverse("condition_alert_list")).content.decode()
    assert "Crear la primera regla" not in body


def test_alert_detail_shows_values_history_and_work_order(client, tenant_a, episode):
    _meter, _rule, alert = episode
    login(client, tenant_a.viewer)
    response = client.get(reverse("condition_alert_detail", args=[alert.pk]))
    body = response.content.decode()
    assert response.status_code == 200
    assert "85 °C" in body and "87 °C" in body
    assert alert.work_order.code in body
    assert "Historial" in body
    assert reverse("condition_alert_acknowledge", args=[alert.pk]) not in body  # el lector no reconoce


def test_acknowledge_with_htmx_returns_the_row_and_records_who(client, tenant_a, episode):
    _meter, _rule, alert = episode
    login(client, tenant_a.admin)
    response = client.post(
        reverse("condition_alert_acknowledge", args=[alert.pk]), headers={"HX-Request": "true"}
    )
    assert response.status_code == 200
    body = response.content.decode()
    assert body.lstrip().startswith("<tr") and "Reconocida" in body
    with tenant_context(tenant_a.org):
        alert.refresh_from_db()
    assert alert.status == ConditionAlert.Status.ACKNOWLEDGED
    assert alert.acknowledged_by == tenant_a.admin.user


def test_acknowledge_without_htmx_redirects_to_detail(client, tenant_a, episode):
    _meter, _rule, alert = episode
    login(client, tenant_a.admin)
    url = reverse("condition_alert_acknowledge", args=[alert.pk])
    response = client.post(url)
    assert response.status_code == 302
    assert response.url == reverse("condition_alert_detail", args=[alert.pk])
    # Reconocer otra vez no hace nada; una normalizada no se reconoce (mensaje, no error 500).
    assert client.post(url).status_code == 302
    with tenant_context(tenant_a.org):
        ConditionAlert.objects.filter(pk=alert.pk).update(status=ConditionAlert.Status.NORMALIZED)
    response = client.post(url, follow=True)
    assert "Solo se reconoce una alerta abierta." in response.content.decode()


def test_acknowledge_is_post_only(client, tenant_a, episode):
    login(client, tenant_a.admin)
    assert client.get(reverse("condition_alert_acknowledge", args=[episode[2].pk])).status_code == 405


# --- Reglas -----------------------------------------------------------------------


def test_rule_list_filters_active_inactive_and_class(client, tenant_a, episode):
    _meter, rule, _alert = episode
    with tenant_context(tenant_a.org):
        class_rule = services.create_rule(
            membership=tenant_a.admin,
            asset_class=tenant_a.asset.asset_class,
            meter_kind=MeterKind.VIBRATION,
            operator="gt",
            threshold="7",
            name="Vibración de compresores",
        )
        services.set_rule_active(membership=tenant_a.admin, rule=rule, is_active=False)
    login(client, tenant_a.admin)
    url = reverse("condition_rule_list")
    edit = reverse("condition_rule_edit", args=[rule.pk])
    body = client.get(url).content.decode()
    assert "Vibración de compresores" in body and edit not in body
    assert edit in client.get(url, {"status": "inactive"}).content.decode()
    assert "Vibración de compresores" not in client.get(url, {"status": "inactive"}).content.decode()
    both = client.get(url, {"status": "all"}).content.decode()
    assert edit in both and "Vibración de compresores" in both
    filtered = client.get(url, {"status": "all", "asset_class": class_rule.asset_class_id}).content.decode()
    assert edit not in filtered and "Vibración de compresores" in filtered


def _rule_data(**overrides):
    data = {
        "scope": "meter",
        "operator": "gt",
        "threshold": "80,5",
        "consecutive": "2",
        "action": "alert_work_order",
        "work_order_priority": "",
    }
    data.update(overrides)
    return data


def test_create_rule_on_a_variable(client, tenant_a):
    with tenant_context(tenant_a.org):
        meter = variable(tenant_a.asset)
    login(client, tenant_a.admin)
    response = client.post(reverse("condition_rule_create"), _rule_data(meter=meter.pk))
    assert response.status_code == 302
    with tenant_context(tenant_a.org):
        rule = ConditionRule.objects.get()
        assert rule.meter == meter and str(rule.threshold) == "80.500" and rule.consecutive == 2
        assert rule.work_order_priority is None  # según la criticidad
        assert AuditEvent.objects.filter(action=AuditEvent.Action.CREATE, target_id=str(rule.pk)).exists()


def test_create_rule_for_a_class(client, tenant_a):
    login(client, tenant_a.admin)
    response = client.post(
        reverse("condition_rule_create"),
        _rule_data(
            scope="class",
            asset_class=tenant_a.asset.asset_class_id,
            meter_kind=MeterKind.PRESSURE,
            meter_name="Descarga",
            operator="outside",
            threshold="",
            min_value="6,5",
            max_value="8",
            action="alert",
            work_order_priority="2",
        ),
    )
    assert response.status_code == 302
    with tenant_context(tenant_a.org):
        rule = ConditionRule.objects.get()
        assert rule.meter is None and rule.asset_class == tenant_a.asset.asset_class
        assert (rule.meter_kind, rule.meter_name, rule.operator) == ("pressure", "Descarga", "outside")
        assert rule.threshold is None and rule.action == "alert" and rule.work_order_priority == 2


def test_create_rule_prefills_the_variable_from_the_url(client, tenant_a):
    with tenant_context(tenant_a.org):
        meter = variable(tenant_a.asset)
    login(client, tenant_a.admin)
    body = client.get(reverse("condition_rule_create"), {"meter": meter.pk}).content.decode()
    assert f'value="{meter.pk}" selected' in body
    assert client.get(reverse("condition_rule_create"), {"meter": "x"}).status_code == 200


@pytest.mark.parametrize(
    ("data", "field", "message"),
    [
        ({"meter": ""}, "meter", "Elige la variable."),
        ({"scope": "class", "asset_class": ""}, "asset_class", "Elige la clase de activo."),
        ({"threshold": ""}, "threshold", "Indica el umbral."),
        ({"threshold": "abc"}, "threshold", "Ingresa un número válido."),
        (
            {"operator": "outside", "min_value": "9", "max_value": "8"},
            "max_value",
            "El máximo debe ser mayor que el mínimo.",
        ),
        ({"operator": "rate", "threshold": "0"}, "threshold", "El cambio por día debe ser mayor que cero."),
    ],
)
def test_create_rule_shows_service_errors_in_their_fields(client, tenant_a, data, field, message):
    with tenant_context(tenant_a.org):
        meter = variable(tenant_a.asset)
    login(client, tenant_a.admin)
    response = client.post(reverse("condition_rule_create"), _rule_data(**{"meter": meter.pk, **data}))
    assert response.status_code == 200
    assert message in response.context["form"].errors[field]
    with tenant_context(tenant_a.org):
        assert not ConditionRule.objects.exists()


def test_create_rule_rejects_a_usage_meter(client, tenant_a):
    with tenant_context(tenant_a.org):
        hours = variable(tenant_a.asset, kind=MeterKind.HOURS, name="")
    login(client, tenant_a.admin)
    response = client.post(reverse("condition_rule_create"), _rule_data(meter=hours.pk))
    assert response.status_code == 200 and "meter" in response.context["form"].errors


def test_edit_rule_changes_condition_but_not_scope(client, tenant_a, episode):
    meter, rule, _alert = episode
    login(client, tenant_a.admin)
    url = reverse("condition_rule_edit", args=[rule.pk])
    body = client.get(url).content.decode()
    assert "El alcance no se cambia" in body and 'name="meter"' not in body
    assert 'value="80"' in body
    response = client.post(url, _rule_data(threshold="90", consecutive="1", name="Rodamiento caliente"))
    assert response.status_code == 302
    with tenant_context(tenant_a.org):
        rule.refresh_from_db()
        assert rule.threshold == 90 and rule.name == "Rodamiento caliente" and rule.meter == meter
    response = client.post(url, _rule_data(threshold=""))
    assert "Indica el umbral." in response.context["form"].errors["threshold"]


def test_deactivate_and_activate_rule(client, tenant_a, episode):
    _meter, rule, alert = episode
    login(client, tenant_a.admin)
    url = reverse("condition_rule_active", args=[rule.pk])
    assert client.post(url, {"is_active": "0"}).status_code == 302
    with tenant_context(tenant_a.org):
        rule.refresh_from_db()
        alert.refresh_from_db()
        assert not rule.is_active and alert.status == ConditionAlert.Status.NORMALIZED
    client.post(url, {"is_active": "1"})
    with tenant_context(tenant_a.org):
        rule.refresh_from_db()
        assert rule.is_active
    assert client.get(url).status_code == 405


# --- Inicio y navegación --------------------------------------------------------------


def test_dashboard_shows_active_condition_alerts_counter(client, tenant_a, episode):
    login(client, tenant_a.tech)
    body = client.get(reverse("dashboard")).content.decode()
    assert "Alertas de condición" in body
    assert reverse("condition_alert_list") in body


def test_menu_links_to_alerts_and_rules(client, tenant_a):
    login(client, tenant_a.viewer)
    body = client.get(reverse("menu")).content.decode()
    assert reverse("condition_alert_list") in body and reverse("condition_rule_list") in body


def test_notification_link_points_to_the_alert_detail(tenant_a, episode):
    from apps.notifications.services import _condition_alert_url

    alert = episode[2]
    assert _condition_alert_url(alert) == reverse("condition_alert_detail", args=[alert.pk])


def test_design_system_shows_condition_components(client, tenant_a):
    tenant_a.admin.user.is_staff = True
    tenant_a.admin.user.save(update_fields=["is_staff"])
    login(client, tenant_a.admin)
    body = client.get(reverse("design_system")).content.decode()
    assert "rule-form" in body and "cond-value" in body and "Reconocida" in body
