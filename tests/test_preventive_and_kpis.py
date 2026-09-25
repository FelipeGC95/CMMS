import datetime

from django.utils import timezone

from apps.assets.models import AssetStatusPeriod
from apps.maintenance.services import assign_plan, generate_due_work_orders
from apps.reporting.selectors import dashboard_kpis
from apps.work.models import FailureEvent, WorkOrder
from apps.work.services import answer_checklist_item, complete_work_order


def test_generation_is_idempotent_and_respects_horizon(in_a, today):
    assign_plan(plan=in_a.plan, asset=in_a.asset, starts_on=today + datetime.timedelta(days=20))
    assert generate_due_work_orders(today=today, horizon_days=14) == []

    created = generate_due_work_orders(today=today, horizon_days=30)
    assert len(created) == 1
    wo = created[0]
    assert wo.work_type == "preventive" and wo.status == "planned"
    assert wo.checklist.count() == 2
    assert generate_due_work_orders(today=today, horizon_days=30) == []


def test_closing_preventive_reschedules_from_real_completion(in_a):
    today = timezone.localdate()
    assignment = assign_plan(plan=in_a.plan, asset=in_a.asset, starts_on=today)
    (wo,) = generate_due_work_orders(today=today)
    for item in wo.checklist.all():
        answer_checklist_item(item=item, membership=in_a.tech, result="ok", value_number="7")
    complete_work_order(work_order=wo, membership=in_a.tech, labor_minutes=60)
    assignment.refresh_from_db()
    assert assignment.last_done_on == today
    assert assignment.next_due_on == today + datetime.timedelta(days=30)
    # Con la OT cerrada, el próximo ciclo sí se genera.
    assert len(generate_due_work_orders(today=today, horizon_days=31)) == 1


def test_kpis_with_known_values(in_a):
    now = timezone.now()
    asset = in_a.asset
    AssetStatusPeriod.objects.create(
        asset=asset,
        status="stopped",
        started_at=now - datetime.timedelta(days=5),
        ended_at=now - datetime.timedelta(days=5) + datetime.timedelta(hours=6),
    )
    FailureEvent.objects.create(
        asset=asset,
        impact="total_stop",
        started_at=now - datetime.timedelta(days=5),
        ended_at=now - datetime.timedelta(days=5) + datetime.timedelta(hours=6),
    )
    WorkOrder.objects.create(
        number=1,
        work_type="preventive",
        status="closed",
        title="a",
        asset=asset,
        due_on=timezone.localdate() - datetime.timedelta(days=3),
        completed_at=now - datetime.timedelta(days=4),
    )
    WorkOrder.objects.create(
        number=2,
        work_type="preventive",
        status="planned",
        title="b",
        asset=asset,
        due_on=timezone.localdate() - datetime.timedelta(days=2),
    )

    kpis = dashboard_kpis(membership=in_a.admin, days=30, now=now)
    window_h = 30 * 24
    assert round(kpis.availability_pct, 4) == round(100 * (1 - 6 / window_h), 4)
    assert kpis.mttr_hours == 6
    assert round(kpis.mtbf_hours, 4) == window_h - 6
    assert kpis.preventive_compliance_pct == 50
    assert kpis.overdue_work_orders == 1
