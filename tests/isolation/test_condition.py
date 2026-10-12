"""Aislamiento del mantenimiento por condición entre organizaciones y por sitio (CBM-08, ADR-026)."""

import pytest
from django.core.exceptions import ObjectDoesNotExist
from django.db import connection

from apps.accounts.models import MembershipLocation
from apps.accounts.permissions import PermissionDeniedError
from apps.assets.models import MeterKind
from apps.condition import selectors, services
from apps.condition.models import ConditionAlert, ConditionRule, ConditionState
from apps.condition.services import ConditionError
from apps.core.tenancy import CrossTenantError, tenant_context
from apps.notifications.models import Kind, Notification
from apps.organizations.models import Location
from tests.condition_utils import read, variable

CROSS = (ConditionError, CrossTenantError, ObjectDoesNotExist, PermissionDeniedError)


def _episode(tenant):
    """Una regla por medidor, una por clase y una alerta con su OT en la organización."""
    with tenant_context(tenant.org):
        meter = variable(tenant.asset)
        rule = services.create_rule(membership=tenant.admin, meter=meter, operator="gt", threshold="80")
        class_rule = services.create_rule(
            membership=tenant.admin,
            asset_class=tenant.asset.asset_class,
            meter_kind=MeterKind.TEMPERATURE,
            meter_name="Rodamiento",
            operator="gt",
            threshold="200",
            action="alert",
        )
        read(meter, "85")
        alert = ConditionAlert.objects.get()
    return meter, rule, class_rule, alert


def test_selectors_only_show_own_rows(tenant_a, tenant_b):
    _episode(tenant_a)
    _meter_b, rule_b, class_rule_b, alert_b = _episode(tenant_b)
    with tenant_context(tenant_a.org):
        alerts = list(selectors.alert_list(membership=tenant_a.admin))
        rules = list(selectors.rule_list(membership=tenant_a.admin, include_inactive=True))
        assert alerts and all(a.organization_id == tenant_a.org.pk for a in alerts)
        assert len(rules) == 2 and all(r.organization_id == tenant_a.org.pk for r in rules)
        assert selectors.alert_for(membership=tenant_a.admin, pk=alert_b.pk) is None
        assert selectors.rule_for(membership=tenant_a.admin, pk=rule_b.pk) is None
        assert selectors.active_alert_count(membership=tenant_a.admin) == 1
        assert {s.organization_id for s in ConditionState.objects.all()} == {tenant_a.org.pk}


def test_services_reject_ids_of_another_organization(tenant_a, tenant_b):
    meter_b, rule_b, _class_rule_b, alert_b = _episode(tenant_b)
    with tenant_context(tenant_a.org):
        a = tenant_a.admin
        attempts = [
            lambda: services.create_rule(membership=a, meter=meter_b, operator="gt", threshold="1"),
            lambda: services.create_rule(
                membership=a,
                asset_class=tenant_b.asset.asset_class,
                meter_kind=MeterKind.TEMPERATURE,
                operator="gt",
                threshold="1",
            ),
            lambda: services.update_rule(membership=a, rule=rule_b, threshold="1"),
            lambda: services.set_rule_active(membership=a, rule=rule_b, is_active=False),
            lambda: services.acknowledge_alert(membership=a, alert=alert_b),
        ]
        for attempt in attempts:
            with pytest.raises(CROSS):
                attempt()
        assert not ConditionRule.objects.exists()
    with tenant_context(tenant_b.org):
        rule_b.refresh_from_db()
        alert_b.refresh_from_db()
        assert rule_b.is_active and rule_b.threshold == 80
        assert alert_b.status == ConditionAlert.Status.OPEN


def test_class_rule_of_one_org_never_evaluates_another_orgs_readings(
    tenant_a, tenant_b, django_capture_on_commit_callbacks
):
    # Misma clase (código CMP), mismo tipo y nombre de variable en ambas organizaciones.
    with tenant_context(tenant_a.org):
        services.create_rule(
            membership=tenant_a.admin,
            asset_class=tenant_a.asset.asset_class,
            meter_kind=MeterKind.TEMPERATURE,
            meter_name="Rodamiento",
            operator="gt",
            threshold="10",
        )
    with tenant_context(tenant_b.org):
        meter_b = variable(tenant_b.asset)
        with django_capture_on_commit_callbacks(execute=True):
            read(meter_b, "99")
        assert not ConditionAlert.objects.exists()
        assert not ConditionState.objects.exists()
    with tenant_context(tenant_a.org):
        assert not ConditionAlert.objects.exists()
        assert not Notification.objects.filter(kind=Kind.CONDITION_ALERT).exists()


def test_alert_notification_stays_in_the_organization(tenant_a, tenant_b, django_capture_on_commit_callbacks):
    with tenant_context(tenant_a.org):
        meter = variable(tenant_a.asset)
        services.create_rule(membership=tenant_a.admin, meter=meter, operator="gt", threshold="80")
        with django_capture_on_commit_callbacks(execute=True):
            read(meter, "85")
        recipients = set(
            Notification.objects.filter(kind=Kind.CONDITION_ALERT).values_list("recipient_id", flat=True)
        )
    assert recipients == {tenant_a.admin.user_id}
    with tenant_context(tenant_b.org):
        assert not Notification.objects.exists()


def test_site_scope_hides_alerts_and_meter_rules_outside_the_scope(tenant_a):
    with tenant_context(tenant_a.org):
        north = Location.objects.create(code="N", name="Norte")
        south = Location.objects.create(code="S", name="Sur")
        tenant_a.asset.location = south
        tenant_a.asset.save(update_fields=["location"])
        meter = variable(tenant_a.asset)
        rule = services.create_rule(membership=tenant_a.admin, meter=meter, operator="gt", threshold="80")
        class_rule = services.create_rule(
            membership=tenant_a.admin,
            asset_class=tenant_a.asset.asset_class,
            meter_kind=MeterKind.TEMPERATURE,
            operator="lt",
            threshold="-50",
        )
        read(meter, "85")
        alert = ConditionAlert.objects.get()
        MembershipLocation.objects.create(membership=tenant_a.viewer, location=north)
        tenant_a.viewer.restricted_to_locations = True
        tenant_a.viewer.save(update_fields=["restricted_to_locations"])
        viewer = tenant_a.viewer
        assert list(selectors.alert_list(membership=viewer)) == []
        assert selectors.alert_for(membership=viewer, pk=alert.pk) is None
        assert list(selectors.rule_list(membership=viewer)) == [class_rule]
        assert selectors.rule_for(membership=viewer, pk=rule.pk) is None
        # Filtro por sitio (incluye sububicaciones) para quien ve todo.
        line = Location.objects.create(code="S1", name="Línea", kind="line", parent=south)
        tenant_a.asset.location = line
        tenant_a.asset.save(update_fields=["location"])
        assert list(selectors.alert_list(membership=tenant_a.admin, location=south)) == [alert]
        assert list(selectors.alert_list(membership=tenant_a.admin, location=north)) == []


@pytest.mark.skipif(connection.vendor != "postgresql", reason="RLS solo existe en PostgreSQL")
def test_rls_hides_condition_rows_of_other_tenant(tenant_a, tenant_b):
    _episode(tenant_a)
    _episode(tenant_b)
    for model in (ConditionRule, ConditionState, ConditionAlert):
        table = connection.ops.quote_name(model._meta.db_table)
        with tenant_context(tenant_a.org), connection.cursor() as cursor:
            cursor.execute(f"SELECT DISTINCT organization_id FROM {table}")
            assert {row[0] for row in cursor.fetchall()} == {tenant_a.org.pk}, model
