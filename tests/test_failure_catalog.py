"""Catálogo de falla y registro al cerrar una OT correctiva (REQ-03)."""

import pytest

from apps.accounts.permissions import PermissionDeniedError
from apps.assets.models import Asset
from apps.core.tenancy import tenant_context
from apps.work.models import FailureCode, FailureEvent, WorkOrder
from apps.work.services import (
    DEFAULT_FAILURE_CATALOG,
    WorkOrderError,
    complete_work_order,
    create_failure_code,
    create_work_order,
    rename_failure_code,
    report_failure,
    seed_default_failure_catalog,
    set_failure_code_active,
)

K = FailureCode.Kind


# --- Catálogo: crear, renombrar, (des)activar ------------------------------------


def test_create_failure_code(in_a):
    code = create_failure_code(kind=K.MECHANISM, code="tst", name="  Prueba  ", membership=in_a.admin)
    assert code.code == "TST" and code.name == "Prueba" and code.is_active


def test_create_failure_code_requires_code_and_name(in_a):
    with pytest.raises(WorkOrderError):
        create_failure_code(kind=K.MODE, code="", name="x", membership=in_a.admin)
    with pytest.raises(WorkOrderError):
        create_failure_code(kind=K.MODE, code="x", name="  ", membership=in_a.admin)


def test_create_failure_code_rejects_duplicate_within_kind(in_a):
    create_failure_code(kind=K.MODE, code="DUP", name="Uno", membership=in_a.admin)
    with pytest.raises(WorkOrderError):
        create_failure_code(kind=K.MODE, code="dup", name="Otro", membership=in_a.admin)
    # mismo código pero otro tipo: no colisiona
    create_failure_code(kind=K.CAUSE, code="DUP", name="Otro tipo", membership=in_a.admin)


def test_rename_failure_code(in_a):
    code = create_failure_code(kind=K.MODE, code="RNM", name="Antes", membership=in_a.admin)
    rename_failure_code(failure_code=code, name="Después", membership=in_a.admin)
    code.refresh_from_db()
    assert code.name == "Después"


def test_rename_failure_code_requires_a_name(in_a):
    code = create_failure_code(kind=K.MODE, code="RNM", name="Antes", membership=in_a.admin)
    with pytest.raises(WorkOrderError):
        rename_failure_code(failure_code=code, name="   ", membership=in_a.admin)


def test_set_failure_code_active_never_deletes(in_a):
    code = create_failure_code(kind=K.MODE, code="TOG", name="x", membership=in_a.admin)
    set_failure_code_active(failure_code=code, is_active=False, membership=in_a.admin)
    code.refresh_from_db()
    assert code.is_active is False
    assert FailureCode.objects.filter(pk=code.pk).exists()  # sigue existiendo, solo desactivado
    set_failure_code_active(failure_code=code, is_active=True, membership=in_a.admin)
    code.refresh_from_db()
    assert code.is_active is True


def test_catalog_actions_require_work_order_manage(in_a):
    code = create_failure_code(kind=K.MODE, code="PRM", name="x", membership=in_a.admin)
    with pytest.raises(PermissionDeniedError):
        create_failure_code(kind=K.MODE, code="Y", name="y", membership=in_a.viewer)
    with pytest.raises(PermissionDeniedError):
        rename_failure_code(failure_code=code, name="y", membership=in_a.viewer)
    with pytest.raises(PermissionDeniedError):
        set_failure_code_active(failure_code=code, is_active=False, membership=in_a.viewer)


# --- Seeding idempotente ----------------------------------------------------------


def test_seed_default_failure_catalog_creates_the_base_set(tenant_a):
    with tenant_context(tenant_a.org):
        codes = set(FailureCode.objects.values_list("kind", "code"))
    assert {(kind, code) for kind, code, _ in DEFAULT_FAILURE_CATALOG} <= codes


def test_seeding_twice_does_not_duplicate(tenant_a):
    with tenant_context(tenant_a.org):
        before = FailureCode.objects.count()
        seed_default_failure_catalog(organization=tenant_a.org)
        seed_default_failure_catalog(organization=tenant_a.org)
        after = FailureCode.objects.count()
    assert before == after == len(DEFAULT_FAILURE_CATALOG)


def test_seeding_does_not_reactivate_or_rename_edited_codes(in_a):
    """Sembrar de nuevo no debe pisar ediciones de la organización."""
    mode_code = DEFAULT_FAILURE_CATALOG[0][1]
    code = FailureCode.objects.get(kind=K.MODE, code=mode_code)
    rename_failure_code(failure_code=code, name="Nombre editado", membership=in_a.admin)
    set_failure_code_active(failure_code=code, is_active=False, membership=in_a.admin)
    seed_default_failure_catalog(organization=in_a.org)
    code.refresh_from_db()
    assert code.name == "Nombre editado"
    assert code.is_active is False


# --- Cierre de OT correctiva (REQ-03 CA2) -----------------------------------------


def test_completing_a_corrective_wo_accepts_the_new_failure_fields(in_a):
    component = Asset.objects.create(
        code="COMP-1", name="Motor", asset_class=in_a.asset.asset_class, parent=in_a.asset
    )
    request = report_failure(membership=in_a.viewer, asset=in_a.asset, description="x", asset_stopped=True)
    mode = FailureCode.objects.filter(kind=K.MODE).first()
    cause = FailureCode.objects.filter(kind=K.CAUSE).first()
    mechanism = FailureCode.objects.filter(kind=K.MECHANISM).first()
    detection = FailureCode.objects.filter(kind=K.DETECTION_METHOD).first()
    complete_work_order(
        work_order=request.work_order,
        membership=in_a.tech,
        labor_minutes=10,
        failure_mode=mode,
        failure_cause=cause,
        failure_mechanism=mechanism,
        detection_method=detection,
        component=component,
    )
    failure = FailureEvent.objects.get(work_order=request.work_order)
    assert failure.failure_mode == mode
    assert failure.failure_cause == cause
    assert failure.failure_mechanism == mechanism
    assert failure.detection_method == detection
    assert failure.component == component


def test_component_must_be_a_child_of_the_work_order_asset(in_a):
    other = Asset.objects.create(code="OTRO", name="Suelto", asset_class=in_a.asset.asset_class)
    request = report_failure(membership=in_a.viewer, asset=in_a.asset, description="x", asset_stopped=True)
    with pytest.raises(WorkOrderError):
        complete_work_order(
            work_order=request.work_order, membership=in_a.tech, labor_minutes=10, component=other
        )


def test_closing_a_corrective_wo_does_not_require_failure_details_by_default(in_a):
    assert in_a.org.require_failure_details is False
    request = report_failure(membership=in_a.viewer, asset=in_a.asset, description="x", asset_stopped=True)
    complete_work_order(work_order=request.work_order, membership=in_a.tech, labor_minutes=10)
    request.work_order.refresh_from_db()
    assert request.work_order.status == WorkOrder.Status.CLOSED


def test_closing_a_corrective_wo_requires_mode_and_cause_when_configured(in_a):
    in_a.org.require_failure_details = True
    in_a.org.save()
    request = report_failure(membership=in_a.viewer, asset=in_a.asset, description="x", asset_stopped=True)
    with pytest.raises(WorkOrderError):
        complete_work_order(work_order=request.work_order, membership=in_a.tech, labor_minutes=10)
    mode = FailureCode.objects.filter(kind=K.MODE).first()
    with pytest.raises(WorkOrderError):
        complete_work_order(
            work_order=request.work_order, membership=in_a.tech, labor_minutes=10, failure_mode=mode
        )
    cause = FailureCode.objects.filter(kind=K.CAUSE).first()
    complete_work_order(
        work_order=request.work_order,
        membership=in_a.tech,
        labor_minutes=10,
        failure_mode=mode,
        failure_cause=cause,
    )
    request.work_order.refresh_from_db()
    assert request.work_order.status == WorkOrder.Status.CLOSED


def test_require_failure_details_only_applies_to_corrective_work_orders_with_a_failure_event(in_a):
    in_a.org.require_failure_details = True
    in_a.org.save()
    wo = create_work_order(work_type="preventive", title="x", asset=in_a.asset)
    complete_work_order(work_order=wo, membership=in_a.tech, labor_minutes=10)
    wo.refresh_from_db()
    assert wo.status == WorkOrder.Status.CLOSED
