"""Horizonte de generación configurable por organización (PM-05 CA1)."""

import datetime as dt

import pytest
from django.core.exceptions import ValidationError
from django.urls import reverse

from apps.audit.models import AuditEvent
from apps.core.tenancy import tenant_context
from apps.maintenance.models import MaintenanceSettings
from apps.maintenance.selectors import maintenance_settings
from apps.maintenance.services import assign_plan, generate_due_work_orders, update_maintenance_settings
from tests.conftest import login


def test_default_horizon_is_14_days_without_creating_a_row(in_a):
    assert maintenance_settings().horizon_days == 14
    assert not MaintenanceSettings.objects.exists()


def test_generation_uses_the_organization_horizon(in_a, today):
    assign_plan(plan=in_a.plan, asset=in_a.asset, starts_on=today + dt.timedelta(days=20))
    assert generate_due_work_orders(today=today) == []  # 20 días > 14 por defecto
    update_maintenance_settings(membership=in_a.admin, horizon_days=21)
    assert len(generate_due_work_orders(today=today)) == 1


def test_update_creates_once_and_audits(in_a):
    update_maintenance_settings(membership=in_a.admin, horizon_days=30)
    update_maintenance_settings(membership=in_a.admin, horizon_days=7)
    assert MaintenanceSettings.objects.get().horizon_days == 7
    assert AuditEvent.objects.filter(target_type__model="maintenancesettings").count() == 2


@pytest.mark.parametrize("value", [0, 91])
def test_horizon_is_bounded(in_a, value):
    with pytest.raises(ValidationError):
        update_maintenance_settings(membership=in_a.admin, horizon_days=value)


def test_settings_screen_saves(client, tenant_a):
    login(client, tenant_a.admin)
    url = reverse("maintenance_settings")
    assert "14" in client.get(url).content.decode()
    assert client.post(url, {"horizon_days": "30"}).status_code == 302
    with tenant_context(tenant_a.org):
        assert maintenance_settings().horizon_days == 30
    assert reverse("maintenance_settings") in client.get(reverse("menu")).content.decode()
