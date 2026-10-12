"""CBM-06 CA4: completar por la API v1 y por terreno registra las lecturas enlazadas (mismo servicio)."""

from decimal import Decimal

import pytest

from apps.assets.models import Meter, MeterKind, MeterReading
from apps.core.tenancy import tenant_context
from apps.work.models import WorkOrder, WorkOrderChecklistItem
from apps.work.services import create_work_order
from tests.api_utils import FIELD_API, api_client, field_client

pytestmark = pytest.mark.django_db


def _linked_wo(tenant):
    with tenant_context(tenant.org):
        wo = create_work_order(work_type=WorkOrder.Type.PREVENTIVE, title="Termografía", asset=tenant.asset)
        item = WorkOrderChecklistItem.objects.create(
            work_order=wo,
            text="Temperatura rodamiento",
            item_type="measurement",
            unit="°C",
            meter_kind=MeterKind.TEMPERATURE,
            meter_name="Rodamiento",
        )
    return wo, item


def _reading(tenant, item):
    with tenant_context(tenant.org):
        item = WorkOrderChecklistItem.objects.select_related("meter_reading__meter").get(pk=item.pk)
        return item.meter_reading


def test_api_v1_answer_and_complete_records_the_reading(tenant_a):
    wo, item = _linked_wo(tenant_a)
    client = api_client(tenant_a)
    base = f"/api/v1/work-orders/{wo.pk}"
    answer = client.post(f"{base}/checklist-items/{item.pk}/answer/", {"value_number": "-3.5"}, format="json")
    assert answer.status_code == 200, answer.content
    assert (answer.json()["meter_kind"], answer.json()["meter_name"]) == ("temperature", "Rodamiento")
    response = client.post(f"{base}/complete/", {"labor_minutes": 10}, format="json")
    assert response.status_code == 200, response.content
    reading = _reading(tenant_a, item)
    assert reading.value == Decimal("-3.5") and reading.source == MeterReading.Source.WORK_ORDER
    assert (reading.meter.kind, reading.meter.name) == ("temperature", "Rodamiento")
    listed = client.get(f"{base}/checklist-items/").json()
    linked = next(i for i in listed["results"] if i["id"] == str(item.pk))
    assert linked["meter_reading"] == str(reading.pk)


def test_field_complete_records_the_reading_once(tenant_a):
    wo, item = _linked_wo(tenant_a)
    client = field_client(tenant_a.tech)
    base = f"{FIELD_API}/work-orders/{wo.pk}"
    assert client.post(f"{base}/checklist-items/{item.pk}/answer/", {"value_number": "71"}).status_code == 200
    body = {"labor_minutes": 15, "client_uuid": "0191f0b4-5c1e-7c2a-9f00-0000000000c6"}
    assert client.post(f"{base}/complete/", body, format="json").status_code == 200
    assert client.post(f"{base}/complete/", body, format="json").status_code == 200  # reintento del teléfono
    reading = _reading(tenant_a, item)
    assert reading.value == Decimal("71") and reading.created_by == tenant_a.tech.user
    with tenant_context(tenant_a.org):
        assert MeterReading.objects.count() == 1
        assert Meter.objects.get(asset=tenant_a.asset).current_value == Decimal("71")
