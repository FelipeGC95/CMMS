"""Permisos de las pantallas de condición (CBM-08): ver con condition.view; crear, editar,
activar y reconocer con condition.manage; externo no entra; sin el módulo, 403 y sin enlaces."""

import pytest
from django.urls import reverse

from apps.accounts.models import Membership
from apps.condition import services
from apps.condition.models import ConditionAlert
from apps.core.tenancy import tenant_context
from apps.organizations.models import Plan
from tests.condition_utils import read, variable
from tests.conftest import login
from tests.inventory_utils import add_member

Role = Membership.Role
# rol: (ve, gestiona)
MATRIX = {
    Role.ADMIN: (True, True),
    Role.PLANNER: (True, True),
    Role.SUPERVISOR: (True, False),
    Role.TECHNICIAN: (True, False),
    Role.VIEWER: (True, False),
    Role.EXTERNAL: (False, False),
}


@pytest.fixture
def episode(tenant_a):
    with tenant_context(tenant_a.org):
        meter = variable(tenant_a.asset)
        rule = services.create_rule(membership=tenant_a.admin, meter=meter, operator="gt", threshold="80")
        read(meter, "85")
        return rule, ConditionAlert.objects.get()


def _member(tenant, role):
    return {
        Role.ADMIN: tenant.admin,
        Role.TECHNICIAN: tenant.tech,
        Role.VIEWER: tenant.viewer,
    }.get(role) or add_member(tenant, role)


@pytest.mark.parametrize("role", list(MATRIX))
def test_screens_follow_the_role_matrix(client, tenant_a, episode, role):
    view, manage = MATRIX[role]
    rule, alert = episode
    login(client, _member(tenant_a, role))
    reads = [
        reverse("condition_alert_list"),
        reverse("condition_alert_detail", args=[alert.pk]),
        reverse("condition_rule_list"),
    ]
    for url in reads:
        assert client.get(url).status_code == (200 if view else 403), url
    writes = [reverse("condition_rule_create"), reverse("condition_rule_edit", args=[rule.pk])]
    for url in writes:
        assert client.get(url).status_code == (200 if manage else 403), url
    ack = client.post(reverse("condition_alert_acknowledge", args=[alert.pk]))
    assert ack.status_code == (302 if manage else 403)
    deactivate = client.post(reverse("condition_rule_active", args=[rule.pk]), {"is_active": "0"})
    assert deactivate.status_code == (302 if manage else 403)
    with tenant_context(tenant_a.org):
        alert.refresh_from_db()
        assert (alert.status != ConditionAlert.Status.OPEN) is manage


@pytest.mark.parametrize("role", [Role.TECHNICIAN, Role.VIEWER])
def test_readers_do_not_see_manage_actions(client, tenant_a, episode, role):
    rule, alert = episode
    login(client, _member(tenant_a, role))
    body = client.get(reverse("condition_alert_list")).content.decode()
    assert reverse("condition_alert_acknowledge", args=[alert.pk]) not in body
    assert reverse("condition_rule_create") not in body
    rules = client.get(reverse("condition_rule_list")).content.decode()
    assert reverse("condition_rule_edit", args=[rule.pk]) not in rules
    assert reverse("condition_rule_active", args=[rule.pk]) not in rules


def test_external_sees_no_counter_and_no_menu_link(client, tenant_a, episode):
    login(client, add_member(tenant_a, Role.EXTERNAL))
    assert reverse("condition_alert_list") not in client.get(reverse("menu")).content.decode()


def test_without_the_module_screens_are_403_and_links_hidden(client, tenant_a, episode):
    rule, alert = episode
    tenant_a.org.plan = Plan.objects.get(code="basic")
    tenant_a.org.save(update_fields=["plan"])
    login(client, tenant_a.admin)
    for url in (
        reverse("condition_alert_list"),
        reverse("condition_alert_detail", args=[alert.pk]),
        reverse("condition_rule_list"),
        reverse("condition_rule_create"),
        reverse("condition_rule_edit", args=[rule.pk]),
    ):
        response = client.get(url)
        assert response.status_code == 403, url
        assert "plan" in response.content.decode().lower()
    assert client.post(reverse("condition_alert_acknowledge", args=[alert.pk])).status_code == 403
    assert reverse("condition_alert_list") not in client.get(reverse("menu")).content.decode()
    assert "Alertas de condición" not in client.get(reverse("dashboard")).content.decode()
