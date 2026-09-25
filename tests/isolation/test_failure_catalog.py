"""Aislamiento del catálogo de falla (REQ-03): la organización A no puede usar
ni editar códigos de falla de la organización B."""

import pytest

from apps.core.tenancy import CrossTenantError, tenant_context
from apps.work.models import FailureCode
from apps.work.services import (
    WorkOrderError,
    complete_work_order,
    rename_failure_code,
    report_failure,
    set_failure_code_active,
)


def test_organization_does_not_see_other_organization_codes(tenant_a, tenant_b):
    with tenant_context(tenant_a.org):
        visible = set(FailureCode.objects.values_list("pk", flat=True))
    with tenant_context(tenant_b.org):
        foreign = set(FailureCode.objects.values_list("pk", flat=True))
    assert visible.isdisjoint(foreign)


def test_cannot_use_a_foreign_failure_code_on_a_failure_event(tenant_a, tenant_b):
    with tenant_context(tenant_b.org):
        foreign_mode = FailureCode.objects.filter(kind=FailureCode.Kind.MODE).first()
    with tenant_context(tenant_a.org):
        request = report_failure(
            membership=tenant_a.viewer, asset=tenant_a.asset, description="x", asset_stopped=True
        )
        with pytest.raises(CrossTenantError):
            complete_work_order(
                work_order=request.work_order,
                membership=tenant_a.tech,
                labor_minutes=10,
                failure_mode=foreign_mode,
            )


def test_cannot_rename_a_foreign_organization_code(tenant_a, tenant_b):
    with tenant_context(tenant_b.org):
        foreign_code = FailureCode.objects.filter(kind=FailureCode.Kind.MODE).first()
    with tenant_context(tenant_a.org), pytest.raises(CrossTenantError):
        rename_failure_code(failure_code=foreign_code, name="hackeado", membership=tenant_a.admin)


def test_cannot_toggle_a_foreign_organization_code(tenant_a, tenant_b):
    with tenant_context(tenant_b.org):
        foreign_code = FailureCode.objects.filter(kind=FailureCode.Kind.MODE).first()
    with tenant_context(tenant_a.org), pytest.raises(CrossTenantError):
        set_failure_code_active(failure_code=foreign_code, is_active=False, membership=tenant_a.admin)


def test_cannot_complete_a_foreign_organization_request(tenant_a, tenant_b):
    """No se puede referenciar ni actuar sobre datos de otra organización al completar la OT."""
    with tenant_context(tenant_b.org):
        foreign = report_failure(
            membership=tenant_b.viewer, asset=tenant_b.asset, description="x", asset_stopped=True
        )
    with tenant_context(tenant_a.org), pytest.raises((CrossTenantError, WorkOrderError)):
        complete_work_order(work_order=foreign.work_order, membership=tenant_a.tech, labor_minutes=10)
