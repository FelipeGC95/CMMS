"""Calendario de preventivos (PM-06): la organización B no ve ni filtra por datos de A."""

import io

from django.urls import reverse
from django.utils import timezone
from openpyxl import load_workbook

from apps.core.tenancy import tenant_context
from apps.maintenance.services import assign_plan, generate_due_work_orders
from tests.conftest import login


def _assign(tenant):
    with tenant_context(tenant.org):
        assign_plan(plan=tenant.plan, asset=tenant.asset, starts_on=timezone.localdate())
        generate_due_work_orders(horizon_days=14)


def test_b_does_not_see_a_projection_or_work_orders(client, tenant_a, tenant_b):
    _assign(tenant_a)
    login(client, tenant_b.admin)
    for params in ({}, {"view": "year"}):
        response = client.get(reverse("plan_calendar"), params)
        assert response.status_code == 200
        assert response.context["result"].occurrences == []
        assert "CMP-01" not in response.content.decode()


def test_b_cannot_filter_by_a_plan_location_or_class(client, tenant_a, tenant_b):
    _assign(tenant_a)
    with tenant_context(tenant_b.org):
        assign_plan(plan=tenant_b.plan, asset=tenant_b.asset, starts_on=timezone.localdate())
    login(client, tenant_b.admin)
    params = {"view": "year", "plan": tenant_a.plan.pk, "asset_class": tenant_a.asset.asset_class_id}
    response = client.get(reverse("plan_calendar"), params)
    assert response.status_code == 200
    assert response.context["filter_form"].errors  # ids de otra organización: no son opciones válidas
    assert {o.asset for o in response.context["result"].occurrences} == {tenant_b.asset}


def test_b_export_has_only_its_rows(client, tenant_a, tenant_b):
    _assign(tenant_a)
    login(client, tenant_b.admin)
    response = client.get(reverse("plan_calendar_export_xlsx"))
    rows = list(load_workbook(io.BytesIO(response.content)).active.iter_rows(values_only=True))
    assert rows[1:] == []
