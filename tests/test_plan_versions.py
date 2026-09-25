"""Versiones inmutables de planes preventivos (PM-01..04, ADR-016)."""

import datetime
from decimal import Decimal

import pytest
from django.utils import timezone

from apps.assets.models import Meter, MeterKind
from apps.assets.services import record_meter_reading
from apps.maintenance.models import ImmutableVersionError, MaintenancePlan, PlanResource, PlanVersion
from apps.maintenance.services import (
    PlanVersionError,
    assign_plan,
    create_plan,
    generate_due_work_orders,
    publish_version,
    register_completion,
    start_draft,
)
from apps.work.models import WorkOrder
from apps.work.services import answer_checklist_item, complete_work_order

ITEM = {"text": "Revisar", "item_type": "check"}


def _complete(wo, membership, **kwargs):
    for item in wo.checklist.all():
        answer_checklist_item(item=item, membership=membership, result="ok", value_number="7")
    return complete_work_order(work_order=wo, membership=membership, labor_minutes=30, **kwargs)


def _new_version(plan, **changes):
    draft = start_draft(plan=plan)
    for field, value in changes.items():
        setattr(draft, field, value)
    draft.save()
    return publish_version(version=draft)


# --- Crear y publicar -------------------------------------------------------


def test_create_plan_publishes_version_one(in_a):
    plan = in_a.plan
    version = plan.current_version
    assert (version.number, version.status) == (1, PlanVersion.Status.PUBLISHED)
    assert version.published_at is not None
    assert plan.interval_days == 30 and plan.frequency_label == "30 días" and plan.items.count() == 2


def test_start_draft_copies_content_and_is_unique(in_a):
    PlanResource.objects.bulk_create(
        [PlanResource(organization=in_a.org, version=in_a.plan.current_version, description="Filtro")]
    )
    draft = start_draft(plan=in_a.plan)
    assert (draft.number, draft.status, draft.interval_days) == (2, "draft", 30)
    assert [i.text for i in draft.items.all()] == ["Purgar", "Presión"]
    assert [r.description for r in draft.resources.all()] == ["Filtro"]
    assert start_draft(plan=in_a.plan) == draft  # un solo borrador por plan


def test_publish_supersedes_previous_and_becomes_current(in_a):
    v1 = in_a.plan.current_version
    v2 = _new_version(in_a.plan, estimated_minutes=90)
    v1.refresh_from_db()
    in_a.plan.refresh_from_db()
    assert v1.status == "superseded" and v2.status == "published"
    assert in_a.plan.current_version == v2 and in_a.plan.estimated_minutes == 90
    with pytest.raises(PlanVersionError):
        publish_version(version=v2)


def test_publish_requires_frequency_and_checklist(in_a):
    draft = start_draft(plan=in_a.plan)
    draft.interval_days = None
    draft.save()
    with pytest.raises(Exception, match="frecuencia"):
        publish_version(version=draft)
    draft.interval_days = 15
    draft.save()
    draft.items.all().delete()
    with pytest.raises(PlanVersionError, match="al menos un ítem"):
        publish_version(version=draft)


def test_plan_without_published_version_is_safe(in_a):
    plan = MaintenancePlan.objects.create(code="NUEVO", name="Sin publicar")
    start_draft(plan=plan)
    plan.refresh_from_db()
    assert (plan.interval_days, plan.uses_meter, plan.frequency_label) == (None, False, "")
    assert list(plan.items.all()) == []
    with pytest.raises(PlanVersionError):
        assign_plan(plan=plan, asset=in_a.asset)


# --- Inmutabilidad ----------------------------------------------------------


def test_published_version_content_cannot_change(in_a):
    version = in_a.plan.current_version
    version.interval_days = 7
    with pytest.raises(ImmutableVersionError):
        version.save()
    item = version.items.first()
    item.text = "Otro"
    with pytest.raises(ImmutableVersionError):
        item.save()
    with pytest.raises(ImmutableVersionError):
        item.delete()
    with pytest.raises(ImmutableVersionError):
        PlanResource.objects.create(version=version, description="Grasa")


def test_draft_content_can_change(in_a):
    draft = start_draft(plan=in_a.plan)
    item = draft.items.first()
    item.text = "Purgar y revisar drenaje"
    item.save()
    PlanResource.objects.create(version=draft, description="Grasa", quantity=Decimal("0.5"), unit="kg")
    draft.items.last().delete()
    assert draft.items.count() == 1 and draft.resources.count() == 1


# --- OT y versiones ---------------------------------------------------------


def test_work_orders_keep_the_version_they_were_generated_with(in_a, today):
    assign_plan(plan=in_a.plan, asset=in_a.asset, starts_on=today)
    (old_wo,) = generate_due_work_orders(today=today)
    assert old_wo.plan_version.number == 1

    draft = start_draft(plan=in_a.plan)
    draft.items.first().delete()
    publish_version(version=draft)

    old_wo.refresh_from_db()
    assert old_wo.plan_version.number == 1 and old_wo.checklist.count() == 2
    old_wo.status = WorkOrder.Status.CANCELLED
    old_wo.save()
    (new_wo,) = generate_due_work_orders(today=today)
    assert new_wo.plan_version == draft and new_wo.checklist.count() == 1


def test_superseded_version_used_by_work_orders_cannot_be_deleted(in_a, today):
    from django.db.models import ProtectedError

    assign_plan(plan=in_a.plan, asset=in_a.asset, starts_on=today)
    (wo,) = generate_due_work_orders(today=today)
    v1 = wo.plan_version
    _new_version(in_a.plan, estimated_minutes=10)
    with pytest.raises(ProtectedError):
        v1.delete()


# --- Recalcular al publicar ---------------------------------------------------


def test_shorter_interval_reschedules_assignments_without_open_work_order(in_a, today):
    assignment = assign_plan(plan=in_a.plan, asset=in_a.asset, starts_on=today + datetime.timedelta(days=20))
    assignment.last_done_on = today - datetime.timedelta(days=10)
    assignment.save()
    _new_version(in_a.plan, interval_days=7)
    assignment.refresh_from_db()
    assert assignment.next_due_on == today - datetime.timedelta(days=3)  # 10 días atrás + 7
    assert len(generate_due_work_orders(today=today)) == 1  # el cambio se nota enseguida


def test_never_done_assignment_uses_start_of_previous_cycle(in_a, today):
    assignment = assign_plan(plan=in_a.plan, asset=in_a.asset, starts_on=today + datetime.timedelta(days=30))
    _new_version(in_a.plan, interval_days=10)
    assignment.refresh_from_db()
    assert assignment.next_due_on == today + datetime.timedelta(days=10)


def test_assignments_with_open_work_order_are_not_touched(in_a, today):
    assignment = assign_plan(plan=in_a.plan, asset=in_a.asset, starts_on=today)
    generate_due_work_orders(today=today)
    _new_version(in_a.plan, interval_days=7)
    assignment.refresh_from_db()
    assert assignment.next_due_on == today


def test_publishing_without_trigger_changes_keeps_schedule(in_a, today):
    assignment = assign_plan(plan=in_a.plan, asset=in_a.asset, starts_on=today + datetime.timedelta(days=5))
    _new_version(in_a.plan, estimated_minutes=45, instructions="Usar EPP")
    assignment.refresh_from_db()
    assert assignment.next_due_on == today + datetime.timedelta(days=5)


# --- Modo fijo ----------------------------------------------------------------


def test_fixed_mode_counts_from_the_scheduled_date(in_a, today):
    plan = create_plan(code="FIX", name="Fijo 30 d", interval_days=30, schedule_mode="fixed", items=[ITEM])
    assignment = assign_plan(plan=plan, asset=in_a.asset, starts_on=today)
    register_completion(assignment=assignment, completed_on=today + datetime.timedelta(days=4))
    assert assignment.next_due_on == today + datetime.timedelta(days=30)


def test_fixed_mode_skips_to_first_target_after_a_long_delay(in_a, today):
    plan = create_plan(code="FIX2", name="Fijo 30 d", interval_days=30, schedule_mode="fixed", items=[ITEM])
    assignment = assign_plan(plan=plan, asset=in_a.asset, starts_on=today)
    register_completion(assignment=assignment, completed_on=today + datetime.timedelta(days=75))
    # Objetivos: +30, +60, +90 → el primero después del día 75 es +90 (sin OT en el pasado).
    assert assignment.next_due_on == today + datetime.timedelta(days=90)


def test_fixed_mode_by_meter(in_a):
    meter = Meter.objects.create(asset=in_a.asset, kind=MeterKind.HOURS)
    record_meter_reading(meter=meter, value=1000)
    plan = create_plan(
        code="FIXH", name="Fijo 250 h", meter_kind="hours", meter_interval=Decimal(250),
        schedule_mode="fixed", items=[ITEM],
    )  # fmt: skip
    assignment = assign_plan(plan=plan, asset=in_a.asset)  # vence a 1250
    register_completion(assignment=assignment, completed_on=timezone.localdate(), meter_value=Decimal("1310"))
    assert assignment.next_due_meter_value == Decimal("1500")
    register_completion(assignment=assignment, completed_on=timezone.localdate(), meter_value=Decimal("2020"))
    assert assignment.next_due_meter_value == Decimal("2250")  # 1500 + 3·250, sin quedar atrás


# --- Medidores contra la versión vigente -----------------------------------------


def test_meter_generation_and_closing_reading_follow_the_current_version(in_a):
    meter = Meter.objects.create(asset=in_a.asset, kind=MeterKind.HOURS)
    record_meter_reading(meter=meter, value=1000)
    plan = create_plan(
        code="H250", name="250 h", meter_kind="hours", meter_interval=Decimal(250), items=[ITEM],
    )  # fmt: skip
    assignment = assign_plan(plan=plan, asset=in_a.asset)
    assert assignment.next_due_meter_value == Decimal("1250")

    v2 = _new_version(plan, meter_interval=Decimal(100))  # nunca ejecutado: 1250 − 250 + 100
    assignment.refresh_from_db()
    assert assignment.next_due_meter_value == Decimal("1100")

    record_meter_reading(meter=meter, value=1095)  # dentro del 10 % previo
    wo = WorkOrder.objects.get()
    assert wo.plan_version == v2 and wo.due_meter_value == Decimal("1100")
    _complete(wo, in_a.tech, meter_value=Decimal("1105"))
    assignment.refresh_from_db()
    assert assignment.next_due_meter_value == Decimal("1205")


# --- Consultas ----------------------------------------------------------------


def test_plan_properties_do_not_query_again(in_a, django_assert_num_queries):
    for i in range(3):
        create_plan(code=f"P{i}", name=f"P{i}", interval_days=10 + i, items=[ITEM])
    with django_assert_num_queries(1):
        labels = [p.frequency_label for p in MaintenancePlan.objects.all()]
    assert len(labels) == 4


# --- Bloqueos (PostgreSQL) ------------------------------------------------------


def test_plan_lock_query_has_no_outer_join(in_a):
    """FOR UPDATE no puede ir sobre un LEFT JOIN en PostgreSQL (select_related de la versión)."""
    from apps.maintenance.services import locked_plans

    queryset = locked_plans().filter(pk=in_a.plan.pk)
    assert queryset.query.select_for_update
    assert not queryset.query.select_related
    assert "JOIN" not in str(queryset.query).upper()


def test_publish_locks_without_joins_on_postgres(in_a):
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    draft = start_draft(plan=in_a.plan)
    with CaptureQueriesContext(connection) as ctx:
        publish_version(version=draft)
    locking = [q["sql"] for q in ctx.captured_queries if "FOR UPDATE" in q["sql"].upper()]
    if connection.vendor == "postgresql":
        assert locking and all("JOIN" not in sql.upper() for sql in locking)
