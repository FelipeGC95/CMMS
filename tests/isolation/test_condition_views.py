"""Aislamiento de las pantallas de condición (CBM-08 CA3): A no ve, edita ni reconoce lo de B,
ni referencia IDs de B en formularios; el alcance por sitio también rige en la web."""

import pytest
from django.urls import reverse

from apps.accounts.models import MembershipLocation
from apps.assets.models import MeterKind
from apps.condition import services
from apps.condition.models import ConditionAlert, ConditionRule
from apps.core.tenancy import tenant_context
from apps.organizations.models import Location
from tests.condition_utils import read, variable
from tests.conftest import login


def _episode(tenant):
    with tenant_context(tenant.org):
        meter = variable(tenant.asset)
        rule = services.create_rule(membership=tenant.admin, meter=meter, operator="gt", threshold="80")
        read(meter, "85")
        return meter, rule, ConditionAlert.objects.get()


def test_lists_only_show_own_alerts_and_rules(client, tenant_a, tenant_b):
    _episode(tenant_a)
    _meter_b, rule_b, alert_b = _episode(tenant_b)
    login(client, tenant_a.admin)
    alerts = client.get(reverse("condition_alert_list"), {"status": "all"}).content.decode()
    rules = client.get(reverse("condition_rule_list"), {"status": "all"}).content.decode()
    assert str(alert_b.pk) not in alerts
    assert str(rule_b.pk) not in rules
    assert "Alertas de condición" in client.get(reverse("dashboard")).content.decode()


def test_detail_edit_acknowledge_and_activate_of_another_org_are_404(client, tenant_a, tenant_b):
    _episode(tenant_a)
    _meter_b, rule_b, alert_b = _episode(tenant_b)
    login(client, tenant_a.admin)
    assert client.get(reverse("condition_alert_detail", args=[alert_b.pk])).status_code == 404
    assert client.post(reverse("condition_alert_acknowledge", args=[alert_b.pk])).status_code == 404
    assert client.get(reverse("condition_rule_edit", args=[rule_b.pk])).status_code == 404
    assert (
        client.post(reverse("condition_rule_edit", args=[rule_b.pk]), {"threshold": "1"}).status_code == 404
    )
    assert (
        client.post(reverse("condition_rule_active", args=[rule_b.pk]), {"is_active": "0"}).status_code == 404
    )
    with tenant_context(tenant_b.org):
        alert_b.refresh_from_db()
        rule_b.refresh_from_db()
        assert alert_b.status == ConditionAlert.Status.OPEN and rule_b.is_active and rule_b.threshold == 80


@pytest.mark.parametrize("field", ["meter", "asset_class"])
def test_rule_form_rejects_ids_of_another_org(client, tenant_a, tenant_b, field):
    meter_b, _rule_b, _alert_b = _episode(tenant_b)
    target = meter_b.pk if field == "meter" else tenant_b.asset.asset_class_id
    login(client, tenant_a.admin)
    data = {
        "scope": "meter" if field == "meter" else "class",
        field: target,
        "meter_kind": MeterKind.TEMPERATURE,
        "operator": "gt",
        "threshold": "80",
        "consecutive": "1",
        "action": "alert",
    }
    response = client.post(reverse("condition_rule_create"), data)
    assert response.status_code == 200 and field in response.context["form"].errors
    with tenant_context(tenant_a.org):
        assert not ConditionRule.objects.exists()


def test_alert_filters_do_not_offer_or_accept_another_org(client, tenant_a, tenant_b):
    _episode(tenant_a)
    _episode(tenant_b)
    with tenant_context(tenant_b.org):
        site_b = Location.objects.create(code="SB", name="Sitio B")
    login(client, tenant_a.admin)
    url = reverse("condition_alert_list")
    body = client.get(url).content.decode()
    assert str(tenant_b.asset.pk) not in body and str(site_b.pk) not in body
    response = client.get(url, {"asset": tenant_b.asset.pk, "location": site_b.pk})
    assert response.status_code == 200
    assert {"asset", "location"} <= set(response.context["filter_form"].errors)


def test_site_scope_hides_alerts_and_variables_outside_the_membership_sites(client, tenant_a):
    _meter, _rule, alert = _episode(tenant_a)
    with tenant_context(tenant_a.org):
        north = Location.objects.create(code="N", name="Norte")
        south = Location.objects.create(code="S", name="Sur")
        tenant_a.asset.location = south
        tenant_a.asset.save(update_fields=["location"])
        planner = tenant_a.admin
        MembershipLocation.objects.create(membership=planner, location=north)
        planner.restricted_to_locations = True
        planner.save(update_fields=["restricted_to_locations"])
    login(client, planner)
    assert client.get(reverse("condition_alert_detail", args=[alert.pk])).status_code == 404
    assert str(alert.pk) not in client.get(reverse("condition_alert_list")).content.decode()
    form = client.get(reverse("condition_rule_create")).context["form"]
    assert list(form.fields["meter"].queryset) == []
