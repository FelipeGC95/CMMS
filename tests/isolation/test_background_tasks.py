"""La tarea de preventivos de una organización no toca datos de otra (NF-07)."""

from apps.core.tenancy import tenant_context
from apps.maintenance import tasks as maintenance_tasks
from apps.maintenance.services import assign_plan
from apps.work.models import WorkOrder


def test_task_generates_preventives_only_for_its_organization(tenant_a, tenant_b, today):
    for tenant in (tenant_a, tenant_b):
        with tenant_context(tenant.org):
            assign_plan(plan=tenant.plan, asset=tenant.asset, starts_on=today)

    created = maintenance_tasks.generate_preventive_work_orders(organization_id=str(tenant_a.org.pk))

    assert created == 1
    with tenant_context(tenant_a.org):
        assert WorkOrder.objects.count() == 1
    with tenant_context(tenant_b.org):
        assert WorkOrder.objects.count() == 0
    # Idempotente (PM-05 CA2): repetir no crea otra OT.
    assert maintenance_tasks.generate_preventive_work_orders(organization_id=str(tenant_a.org.pk)) == 0
