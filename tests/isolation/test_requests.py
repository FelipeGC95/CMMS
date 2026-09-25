"""Aislamiento de la triage de solicitudes (REQ-01 CA3): la organización A no
puede decidir ni fusionar solicitudes de la organización B."""

import pytest

from apps.core.tenancy import CrossTenantError, tenant_context
from apps.work.models import WorkRequest
from apps.work.services import WorkOrderError, accept_request, merge_request, reject_request, report_failure


def _pending_request(tenant):
    with tenant_context(tenant.org):
        return report_failure(
            membership=tenant.viewer, asset=tenant.asset, description="x", asset_stopped=False
        )


def test_cannot_accept_a_foreign_organization_request(tenant_a, tenant_b):
    foreign = _pending_request(tenant_b)
    with tenant_context(tenant_a.org), pytest.raises(CrossTenantError):
        accept_request(work_request=foreign, membership=tenant_a.admin)


def test_cannot_reject_a_foreign_organization_request(tenant_a, tenant_b):
    foreign = _pending_request(tenant_b)
    with tenant_context(tenant_a.org), pytest.raises(CrossTenantError):
        reject_request(work_request=foreign, membership=tenant_a.admin, reason="No corresponde")


def test_cannot_merge_with_a_foreign_organization_request(tenant_a, tenant_b):
    own = _pending_request(tenant_a)
    foreign = _pending_request(tenant_b)
    with tenant_context(tenant_a.org):
        foreign_seen_from_a = WorkRequest.all_tenants.get(pk=foreign.pk)
        with pytest.raises(WorkOrderError):
            merge_request(work_request=own, into=foreign_seen_from_a, membership=tenant_a.admin)
