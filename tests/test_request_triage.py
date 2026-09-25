"""Decisión del planificador sobre solicitudes (REQ-01 CA3): reglas de negocio."""

import pytest

from apps.assets.models import Asset
from apps.work.models import FailureEvent, WorkOrder, WorkRequest
from apps.work.services import (
    WorkOrderError,
    accept_request,
    complete_work_order,
    merge_request,
    reject_request,
    report_failure,
)

R = WorkRequest.Status


# --- Aceptar ----------------------------------------------------------------------


def test_accept_request_marks_decided_and_keeps_the_existing_work_order(in_a):
    request = report_failure(
        membership=in_a.viewer, asset=in_a.asset, description="Ruido extraño", asset_stopped=False
    )
    wo_id = request.work_order_id
    accept_request(work_request=request, membership=in_a.admin)
    request.refresh_from_db()
    assert request.status == R.ACCEPTED
    assert request.decided_by == in_a.admin.user
    assert request.decided_at is not None
    assert request.work_order_id == wo_id  # no se crea una OT nueva: se reutiliza la del aviso


def test_cannot_decide_twice(in_a):
    request = report_failure(membership=in_a.viewer, asset=in_a.asset, description="x", asset_stopped=False)
    accept_request(work_request=request, membership=in_a.admin)
    with pytest.raises(WorkOrderError):
        accept_request(work_request=request, membership=in_a.admin)
    with pytest.raises(WorkOrderError):
        reject_request(work_request=request, membership=in_a.admin, reason="motivo")


# --- Rechazar -----------------------------------------------------------------------


def test_reject_request_requires_a_reason(in_a):
    request = report_failure(membership=in_a.viewer, asset=in_a.asset, description="x", asset_stopped=False)
    with pytest.raises(WorkOrderError):
        reject_request(work_request=request, membership=in_a.admin, reason="   ")
    request.refresh_from_db()
    assert request.status == R.PENDING


def test_reject_request_cancels_its_work_order(in_a):
    request = report_failure(membership=in_a.viewer, asset=in_a.asset, description="x", asset_stopped=False)
    reject_request(work_request=request, membership=in_a.admin, reason="Duplicada de otro aviso")
    request.refresh_from_db()
    assert request.status == R.REJECTED
    assert request.rejection_reason == "Duplicada de otro aviso"
    request.work_order.refresh_from_db()
    assert request.work_order.status == WorkOrder.Status.CANCELLED


def test_reject_request_does_not_touch_the_failure_event_or_asset_status(in_a):
    """El evento de falla y el estado del activo son la condición física real:
    rechazar el aviso (la OT administrativa) no los revierte."""
    request = report_failure(
        membership=in_a.viewer, asset=in_a.asset, description="Fuga de aceite", asset_stopped=True
    )
    reject_request(work_request=request, membership=in_a.admin, reason="No es un problema real")
    in_a.asset.refresh_from_db()
    assert in_a.asset.status == Asset.Status.STOPPED
    failure = FailureEvent.objects.get(work_order=request.work_order)
    assert failure.ended_at is None


def test_cannot_reject_a_request_whose_work_order_is_already_closed(in_a):
    request = report_failure(membership=in_a.viewer, asset=in_a.asset, description="x", asset_stopped=False)
    complete_work_order(work_order=request.work_order, membership=in_a.tech, labor_minutes=10)
    with pytest.raises(WorkOrderError):
        reject_request(work_request=request, membership=in_a.admin, reason="tarde")


# --- Fusionar -----------------------------------------------------------------------


def test_merge_request_cancels_the_source_and_keeps_the_target(in_a):
    first = report_failure(membership=in_a.viewer, asset=in_a.asset, description="Ruido", asset_stopped=False)
    second = report_failure(
        membership=in_a.viewer, asset=in_a.asset, description="El mismo ruido", asset_stopped=False
    )
    merge_request(work_request=second, into=first, membership=in_a.admin)
    second.refresh_from_db()
    first.refresh_from_db()
    assert second.status == R.MERGED
    assert second.merged_into_id == first.pk
    second.work_order.refresh_from_db()
    assert second.work_order.status == WorkOrder.Status.CANCELLED
    assert first.status == R.PENDING
    assert first.work_order.status == WorkOrder.Status.REQUESTED


def test_merge_request_requires_the_same_asset(in_a):
    other_class = in_a.asset.asset_class
    other_asset = Asset.objects.create(code="OTRO", name="Otro activo", asset_class=other_class)
    first = report_failure(membership=in_a.viewer, asset=in_a.asset, description="x", asset_stopped=False)
    second = report_failure(membership=in_a.viewer, asset=other_asset, description="y", asset_stopped=False)
    with pytest.raises(WorkOrderError):
        merge_request(work_request=second, into=first, membership=in_a.admin)


def test_merge_request_requires_the_target_to_be_open(in_a):
    first = report_failure(membership=in_a.viewer, asset=in_a.asset, description="x", asset_stopped=False)
    second = report_failure(membership=in_a.viewer, asset=in_a.asset, description="y", asset_stopped=False)
    third = report_failure(membership=in_a.viewer, asset=in_a.asset, description="z", asset_stopped=False)
    reject_request(work_request=first, membership=in_a.admin, reason="no corresponde")
    with pytest.raises(WorkOrderError):
        merge_request(work_request=third, into=first, membership=in_a.admin)
    merge_request(work_request=third, into=second, membership=in_a.admin)  # sigue abierta: funciona


def test_merge_request_cannot_target_itself(in_a):
    request = report_failure(membership=in_a.viewer, asset=in_a.asset, description="x", asset_stopped=False)
    with pytest.raises(WorkOrderError):
        merge_request(work_request=request, into=request, membership=in_a.admin)
