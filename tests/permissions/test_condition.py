"""Permisos y módulo del mantenimiento por condición (CBM-08, ADR-013, ORG-10)."""

import pytest

from apps.accounts.models import Membership
from apps.accounts.permissions import Perm, PermissionDeniedError, has_perm
from apps.assets.models import MeterKind
from apps.condition import selectors, services
from apps.condition.models import ConditionAlert
from apps.core.tenancy import tenant_context
from apps.notifications.models import Kind
from apps.notifications.selectors import receivable_kinds
from apps.organizations.models import Plan
from apps.organizations.modules import API_MODULES, has_module, module_for_path
from apps.organizations.plans import DEFAULT_PLANS
from tests.condition_utils import read, variable
from tests.inventory_utils import add_member

Role = Membership.Role

# rol: (condition.view, condition.manage, recibe condition_alert)
MATRIX = {
    Role.ADMIN: (True, True, True),
    Role.PLANNER: (True, True, True),
    Role.SUPERVISOR: (True, False, True),
    Role.TECHNICIAN: (True, False, False),
    Role.VIEWER: (True, False, False),
    Role.EXTERNAL: (False, False, False),
}


@pytest.mark.parametrize("role", list(MATRIX))
def test_role_matrix(role):
    view, manage, _kind = MATRIX[role]
    membership = Membership(role=role)
    assert has_perm(membership, Perm.CONDITION_VIEW) is view
    assert has_perm(membership, Perm.CONDITION_MANAGE) is manage


@pytest.fixture
def members(tenant_a):
    return {
        Role.ADMIN: tenant_a.admin,
        Role.PLANNER: add_member(tenant_a, Role.PLANNER),
        Role.SUPERVISOR: add_member(tenant_a, Role.SUPERVISOR),
        Role.TECHNICIAN: tenant_a.tech,
        Role.VIEWER: tenant_a.viewer,
        Role.EXTERNAL: add_member(tenant_a, Role.EXTERNAL),
    }


@pytest.fixture
def alert(tenant_a):
    with tenant_context(tenant_a.org):
        meter = variable(tenant_a.asset)
        services.create_rule(
            membership=tenant_a.admin, meter=meter, operator="gt", threshold="80", action="alert"
        )
        read(meter, "85")
        return ConditionAlert.objects.get()


@pytest.mark.parametrize("role", list(MATRIX))
def test_services_and_selectors_enforce_permissions(tenant_a, members, alert, role):
    view, manage, _kind = MATRIX[role]
    membership = members[role]
    with tenant_context(tenant_a.org):
        if view:
            assert list(selectors.alert_list(membership=membership)) == [alert]
            assert len(list(selectors.rule_list(membership=membership))) == 1
        else:
            with pytest.raises(PermissionDeniedError):
                selectors.alert_list(membership=membership)
            with pytest.raises(PermissionDeniedError):
                selectors.rule_list(membership=membership)
        create = lambda: services.create_rule(  # noqa: E731
            membership=membership,
            asset_class=tenant_a.asset.asset_class,
            meter_kind=MeterKind.VIBRATION,
            operator="gt",
            threshold="7",
        )
        acknowledge = lambda: services.acknowledge_alert(membership=membership, alert=alert)  # noqa: E731
        if manage:
            create()
            assert acknowledge().status == ConditionAlert.Status.ACKNOWLEDGED
        else:
            for attempt in (create, acknowledge):
                with pytest.raises(PermissionDeniedError):
                    attempt()


@pytest.mark.parametrize("role", list(MATRIX))
def test_condition_alert_kind_only_for_roles_that_manage_work_orders(tenant_a, members, role):
    membership = members[role]
    membership.organization = tenant_a.org
    assert (Kind.CONDITION_ALERT in receivable_kinds(membership=membership)) is MATRIX[role][2]


def test_condition_alert_kind_hidden_without_the_module(tenant_a):
    tenant_a.org.plan = Plan.objects.get(code="basic")
    tenant_a.org.save(update_fields=["plan"])
    tenant_a.admin.organization = tenant_a.org
    assert Kind.CONDITION_ALERT not in receivable_kinds(membership=tenant_a.admin)


def test_condition_module_in_trial_pro_and_enterprise_but_not_basic(db):
    extras = {code: extra for code, _name, *_quota, extra in DEFAULT_PLANS}
    assert {code for code, extra in extras.items() if "condition" in extra} == {"trial", "pro", "enterprise"}
    # La migración de datos lo agregó a los planes existentes, sin duplicar.
    for plan in Plan.objects.filter(code__in=["trial", "pro", "enterprise"]):
        assert plan.features.count("condition") == 1
    assert "condition" not in Plan.objects.get(code="basic").features


def test_condition_routes_and_api_belong_to_the_module():
    assert module_for_path("/condition/") == "condition"
    assert module_for_path("/condition/rules/abc/") == "condition"
    assert {API_MODULES[name] for name in ("api-condition-rule", "api-condition-alert")} == {"condition"}


def test_condition_urls_are_forbidden_without_the_module(client, tenant_a):
    from tests.conftest import login

    tenant_a.org.plan = Plan.objects.get(code="basic")
    tenant_a.org.save(update_fields=["plan"])
    assert not has_module(tenant_a.org, "condition")
    login(client, tenant_a.admin)
    assert client.get("/condition/").status_code == 403
