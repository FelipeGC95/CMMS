"""CBM-06: enlazar es de quien edita planes; registrar la lectura al completar, de quien ejecuta la OT."""

from decimal import Decimal

import pytest
from django.urls import reverse

from apps.accounts.permissions import Perm, PermissionDeniedError, has_perm
from apps.assets.models import Meter, MeterKind, MeterReading
from apps.core.tenancy import tenant_context
from apps.maintenance import services as plans
from apps.maintenance.models import ItemType
from apps.work.models import WorkOrder, WorkOrderChecklistItem
from apps.work.services import complete_work_order, create_work_order
from tests.conftest import login

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize("role", ["tech", "viewer"])
def test_only_planners_link_items_to_variables(client, tenant_a, role):
    membership = getattr(tenant_a, role)
    with tenant_context(tenant_a.org):
        draft = plans.create_draft(membership=tenant_a.admin, plan=tenant_a.plan)
        item = draft.items.get(text="Presión")
        with pytest.raises(PermissionDeniedError):
            plans.update_draft_item(
                membership=membership,
                item=item,
                text="Presión",
                item_type=ItemType.MEASUREMENT,
                meter_kind=MeterKind.PRESSURE,
            )
    login(client, membership)
    data = {"text": "Presión", "item_type": "measurement", "meter_kind": "pressure"}
    response = client.post(reverse("plan_item_edit", args=[tenant_a.plan.pk, item.pk]), data)
    assert response.status_code == 403
    with tenant_context(tenant_a.org):
        item.refresh_from_db()
        assert item.meter_kind == ""


def test_technician_records_the_reading_and_creates_the_variable_without_asset_manage(tenant_a):
    tech = tenant_a.tech
    assert not has_perm(tech, Perm.ASSET_MANAGE) and has_perm(tech, Perm.METER_READING_CREATE)
    with tenant_context(tenant_a.org):
        wo = create_work_order(work_type=WorkOrder.Type.PREVENTIVE, title="Termografía", asset=tenant_a.asset)
        WorkOrderChecklistItem.objects.create(
            work_order=wo,
            text="Corriente",
            item_type="measurement",
            unit="A",
            value_number=Decimal("12.5"),
            result="ok",
            meter_kind=MeterKind.CURRENT,
            meter_name="Motor",
        )
        complete_work_order(work_order=wo, membership=tech, labor_minutes=0)
        meter = Meter.objects.get(kind=MeterKind.CURRENT, name="Motor")
        assert MeterReading.objects.get(meter=meter).created_by == tech.user
