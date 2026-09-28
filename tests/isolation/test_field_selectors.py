"""Modo sin conexión (X-06): la organización B no ve, edita ni referencia datos de A."""

import datetime
import uuid

import pytest
from django.utils import timezone

from apps.assets.models import Meter, MeterKind
from apps.audit.models import AuditEvent
from apps.core.tenancy import tenant_context
from apps.work.models import FailureCode, LaborEntry
from apps.work.selectors import field_reference, field_work_orders
from apps.work.services import add_checklist_item, answer_checklist_item, create_work_order, log_labor


def _wo(tenant):
    with tenant_context(tenant.org):
        return create_work_order(
            work_type="corrective", title="OT", asset=tenant.asset, assigned_to=tenant.tech.user
        )


def test_snapshot_only_brings_work_orders_of_the_active_organization(tenant_a, tenant_b):
    wo_a, wo_b = _wo(tenant_a), _wo(tenant_b)
    with tenant_context(tenant_b.org):
        ids = {wo.pk for wo in field_work_orders(membership=tenant_b.tech).work_orders}
    assert ids == {wo_b.pk}
    assert wo_a.pk not in ids


def test_reference_only_brings_catalogs_of_the_active_organization(tenant_a, tenant_b):
    with tenant_context(tenant_a.org):
        meter_a = Meter.objects.create(asset=tenant_a.asset, kind=MeterKind.HOURS)
        code_a = FailureCode.objects.create(kind=FailureCode.Kind.MODE, code="SOLOA", name="Solo A")
    with tenant_context(tenant_b.org):
        reference = field_reference(membership=tenant_b.tech)
    assert {a["id"] for a in reference.assets} == {tenant_b.asset.pk}
    assert meter_a.pk not in {m["id"] for m in reference.meters}
    codes = {c["id"] for kind in reference.failure_codes.values() for c in kind}
    assert code_a.pk not in codes


def test_labor_client_uuid_of_a_is_not_found_from_b(tenant_a, tenant_b):
    """Reusar la clave de A en B no devuelve el registro de A: crea el de B."""
    key = uuid.uuid4()
    wo_a, wo_b = _wo(tenant_a), _wo(tenant_b)
    with tenant_context(tenant_a.org):
        entry_a = log_labor(work_order=wo_a, membership=tenant_a.tech, minutes=30, client_uuid=key)
    with tenant_context(tenant_b.org):
        entry_b = log_labor(work_order=wo_b, membership=tenant_b.tech, minutes=15, client_uuid=key)
        assert entry_b.pk != entry_a.pk and entry_b.organization_id == tenant_b.org.pk
        assert list(LaborEntry.objects.values_list("pk", flat=True)) == [entry_b.pk]


def test_checklist_answer_audit_stays_in_the_organization(tenant_a, tenant_b):
    wo_a = _wo(tenant_a)
    with tenant_context(tenant_a.org):
        item = add_checklist_item(work_order=wo_a, membership=tenant_a.admin, text="Revisar")
        answer_checklist_item(item=item, membership=tenant_a.tech, result="ok")
        late = answer_checklist_item(
            item=item,
            membership=tenant_a.tech,
            result="not_ok",
            answered_at=timezone.now() - datetime.timedelta(hours=1),
        )
        assert late.superseded
        assert AuditEvent.objects.filter(action=AuditEvent.Action.CHECKLIST_ANSWER).count() == 1
    with tenant_context(tenant_b.org):
        assert not AuditEvent.objects.filter(action=AuditEvent.Action.CHECKLIST_ANSWER).exists()


def test_selectors_require_tenant_context(tenant_a):
    from apps.core.tenancy import TenantNotSet

    with pytest.raises(TenantNotSet):
        field_work_orders(membership=tenant_a.tech)
