import uuid

import pytest
from django.urls import reverse

from apps.accounts.permissions import PermissionDeniedError
from apps.assets.models import Asset, AssetStatusPeriod
from apps.maintenance.services import publish_version, start_draft
from apps.work.models import FailureEvent, WorkOrder
from apps.work.services import (
    WorkOrderError,
    answer_checklist_item,
    complete_work_order,
    create_work_order,
    report_failure,
    transition,
)
from tests.conftest import login


def test_report_failure_creates_corrective_and_stops_asset(in_a):
    request = report_failure(
        membership=in_a.viewer, asset=in_a.asset, description="Pierde aceite\nmás detalle", asset_stopped=True
    )
    wo = request.work_order
    assert wo.work_type == "corrective" and wo.status == "requested"
    assert wo.title == "Pierde aceite"
    assert wo.priority == WorkOrder.Priority.URGENT  # detenido + criticidad A
    in_a.asset.refresh_from_db()
    assert in_a.asset.status == Asset.Status.STOPPED
    assert FailureEvent.objects.get(work_order=wo).ended_at is None


def test_report_failure_is_idempotent_by_client_uuid(in_a):
    key = uuid.uuid4()
    first = report_failure(
        membership=in_a.viewer, asset=in_a.asset, description="x", asset_stopped=False, client_uuid=key
    )
    second = report_failure(
        membership=in_a.viewer, asset=in_a.asset, description="x", asset_stopped=False, client_uuid=key
    )
    assert first.pk == second.pk
    assert WorkOrder.objects.count() == 1


def test_completing_corrective_restores_asset_and_closes_failure(in_a):
    wo = report_failure(
        membership=in_a.viewer, asset=in_a.asset, description="x", asset_stopped=True
    ).work_order
    complete_work_order(work_order=wo, membership=in_a.tech, labor_minutes=45, notes="Cambio de sello")
    wo.refresh_from_db()
    in_a.asset.refresh_from_db()
    assert wo.status == "closed" and wo.closed_at is not None
    assert in_a.asset.status == Asset.Status.OPERATIONAL
    assert FailureEvent.objects.get(work_order=wo).ended_at is not None
    statuses = list(AssetStatusPeriod.objects.order_by("started_at").values_list("status", flat=True))
    assert statuses == ["stopped", "in_maintenance", "operational"]
    assert AssetStatusPeriod.objects.filter(ended_at__isnull=True).count() == 1


def test_close_approval_keeps_work_order_completed(in_a):
    in_a.org.require_close_approval = True
    in_a.org.save()
    wo = create_work_order(work_type="corrective", title="x", asset=in_a.asset)
    complete_work_order(work_order=wo, membership=in_a.tech, labor_minutes=10)
    assert wo.status == "completed"
    transition(work_order=wo, to_status="closed", membership=in_a.admin)
    assert wo.status == "closed"


def test_numbers_are_sequential_per_type(in_a):
    c1 = create_work_order(work_type="corrective", title="a", asset=in_a.asset)
    c2 = create_work_order(work_type="corrective", title="b", asset=in_a.asset)
    p1 = create_work_order(work_type="preventive", title="c", asset=in_a.asset)
    assert (c1.number, c2.number, p1.number) == (1, 2, 1)
    assert c2.code == "OT-C-00002" and p1.code == "OT-P-00001"


def test_invalid_transition_is_rejected(in_a):
    wo = create_work_order(work_type="corrective", title="x", asset=in_a.asset)
    with pytest.raises(WorkOrderError):
        transition(work_order=wo, to_status="closed", membership=in_a.admin)


def test_technician_cannot_cancel(in_a):
    wo = create_work_order(work_type="corrective", title="x", asset=in_a.asset)
    with pytest.raises(WorkOrderError):
        transition(work_order=wo, to_status="cancelled", membership=in_a.tech)


def test_viewer_cannot_execute_checklist(in_a):
    wo = create_work_order(
        work_type="preventive", title="x", asset=in_a.asset, checklist=in_a.plan.items.all()
    )
    with pytest.raises(PermissionDeniedError):
        answer_checklist_item(item=wo.checklist.first(), membership=in_a.viewer, result="ok")


def test_checklist_snapshot_measurement_and_required_items(in_a):
    wo = create_work_order(
        work_type="preventive", title="x", asset=in_a.asset, checklist=in_a.plan.items.all()
    )
    check, measure = wo.checklist.all()
    # Publicar una versión nueva del plan no altera la OT ya generada.
    draft = start_draft(plan=in_a.plan)
    item = draft.items.get(order=1)
    item.text = "Otro texto"
    item.save()
    publish_version(version=draft)
    check.refresh_from_db()
    assert check.text == "Purgar"

    answer_checklist_item(item=measure, membership=in_a.tech, value_number="9.2")
    measure.refresh_from_db()
    wo.refresh_from_db()
    assert measure.result == "not_ok"  # fuera de rango 6,5–8
    assert wo.status == "in_progress"  # la primera respuesta inicia la OT

    with pytest.raises(WorkOrderError):
        complete_work_order(work_order=wo, membership=in_a.tech, labor_minutes=30)

    answer_checklist_item(item=check, membership=in_a.tech, result="ok")
    complete_work_order(work_order=wo, membership=in_a.tech, labor_minutes=30)
    assert wo.status == "closed"


def test_report_view_flow(client, tenant_a):
    login(client, tenant_a.viewer)
    response = client.post(
        reverse("asset_report", args=[tenant_a.asset.pk]),
        {"description": "Ruido fuerte", "asset_stopped": "on", "client_uuid": str(uuid.uuid4())},
    )
    assert response.status_code == 302
    detail = client.get(response["Location"])
    assert detail.status_code == 200
    assert "Ruido fuerte" in detail.content.decode()


def test_checklist_htmx_view_returns_partial(client, in_a):
    wo = create_work_order(
        work_type="preventive", title="x", asset=in_a.asset, checklist=in_a.plan.items.all()
    )
    item = wo.checklist.first()
    login(client, in_a.tech)
    response = client.post(
        reverse("checklist_answer", args=[wo.pk, item.pk]), {"result": "ok"}, HTTP_HX_REQUEST="true"
    )
    assert response.status_code == 200
    assert "check-item--ok" in response.content.decode()
