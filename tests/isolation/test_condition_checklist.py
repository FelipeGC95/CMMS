"""CBM-06: el checklist de una organización no enlaza ni registra lecturas en variables de otra."""

from decimal import Decimal

import pytest
from django.urls import reverse

from apps.assets.models import Meter, MeterKind, MeterReading
from apps.core.tenancy import tenant_context
from apps.maintenance import services as plans
from apps.work.models import WorkOrder, WorkOrderChecklistItem
from apps.work.services import complete_work_order, create_work_order, record_checklist_readings
from tests.condition_utils import variable
from tests.conftest import login

pytestmark = pytest.mark.django_db


def _linked_wo(tenant, value="50"):
    with tenant_context(tenant.org):
        wo = create_work_order(work_type=WorkOrder.Type.PREVENTIVE, title="Termografía", asset=tenant.asset)
        WorkOrderChecklistItem.objects.create(
            work_order=wo,
            text="Temperatura",
            item_type="measurement",
            unit="°C",
            value_number=Decimal(value),
            result="ok",
            meter_kind=MeterKind.TEMPERATURE,
            meter_name="Rodamiento",
        )
    return wo


def test_completing_uses_only_variables_of_its_own_asset(tenant_a, tenant_b):
    with tenant_context(tenant_b.org):
        meter_b = variable(tenant_b.asset)  # misma clave (tipo, nombre) en la otra organización
    wo = _linked_wo(tenant_a)
    with tenant_context(tenant_a.org):
        complete_work_order(work_order=wo, membership=tenant_a.tech, labor_minutes=0)
        meter_a = Meter.objects.get()
        assert meter_a.asset_id == tenant_a.asset.pk and meter_a.pk != meter_b.pk
        assert MeterReading.objects.get().meter_id == meter_a.pk
    with tenant_context(tenant_b.org):
        assert not MeterReading.objects.exists()
        assert Meter.objects.get().current_value is None


def test_another_organization_cannot_record_readings_of_a_work_order(tenant_a, tenant_b):
    wo_b = _linked_wo(tenant_b)
    with tenant_context(tenant_a.org):
        assert record_checklist_readings(work_order=wo_b, membership=tenant_a.admin) == []
    with tenant_context(tenant_b.org):
        assert not MeterReading.objects.exists() and not Meter.objects.exists()
        assert WorkOrderChecklistItem.objects.get().meter_reading_id is None


def test_plan_editor_cannot_link_an_item_of_another_organization(client, tenant_a, tenant_b):
    with tenant_context(tenant_b.org):
        draft_b = plans.create_draft(membership=tenant_b.admin, plan=tenant_b.plan)
        item_b = draft_b.items.get(text="Presión")
    with tenant_context(tenant_a.org):
        plans.create_draft(membership=tenant_a.admin, plan=tenant_a.plan)
    login(client, tenant_a.admin)
    data = {"text": "Presión", "item_type": "measurement", "meter_kind": "pressure", "meter_name": "X"}
    for plan_pk in (tenant_a.plan.pk, tenant_b.plan.pk):
        response = client.post(reverse("plan_item_edit", args=[plan_pk, item_b.pk]), data)
        assert response.status_code == 404
    with tenant_context(tenant_b.org):
        item_b.refresh_from_db()
        assert item_b.meter_kind == ""
