"""Pantallas de planes preventivos (PM-01..PM-04): servicios con base de datos real y vistas."""

import datetime
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.urls import reverse

from apps.accounts.models import Membership, User
from apps.assets.models import Asset, AssetClass, Meter
from apps.assets.services import create_asset
from apps.audit.models import AuditEvent
from apps.core.tenancy import tenant_context
from apps.maintenance import services
from apps.maintenance.models import ItemType, MaintenancePlan, PlanAssignment, PlanVersion
from apps.maintenance.services import PlanVersionError, create_plan, generate_due_work_orders
from tests.conftest import login


def _planner(tenant):
    user = User.objects.create_user(f"planner@{tenant.org.slug}.cl", "x", full_name="Planificador")
    return Membership.objects.create(user=user, organization=tenant.org, role="planner")


def _new_plan(membership, **overrides):
    fields = {"code": "PM-NEW", "name": "Nuevo", "calendar_interval": 30, **overrides}
    return services.create_plan_with_draft(membership=membership, **fields)


# --- Servicios ----------------------------------------------------------------


def test_create_plan_starts_as_draft_and_publishes_with_items(in_a):
    plan = _new_plan(in_a.admin)
    assert plan.current_version is None
    draft = plan.versions.get()
    assert draft.is_draft and draft.number == 1
    with pytest.raises(PlanVersionError):
        services.publish_draft(membership=in_a.admin, version=draft)
    services.add_draft_item(membership=in_a.admin, version=draft, text="Revisar", item_type=ItemType.CHECK)
    services.publish_draft(membership=in_a.admin, version=draft, change_note="Primera")
    plan.refresh_from_db()
    assert plan.current_version_id == draft.pk
    assert plan.current_version.change_note == "Primera"
    assert AuditEvent.objects.filter(target_id=plan.pk, action=AuditEvent.Action.CREATE).exists()


def test_duplicate_code_is_rejected(in_a):
    with pytest.raises(ValidationError):
        _new_plan(in_a.admin, code="pm1")  # conftest ya tiene PM1


def test_draft_copies_current_version_and_is_idempotent(in_a):
    plan = in_a.plan
    draft = services.create_draft(membership=in_a.admin, plan=plan)
    assert services.create_draft(membership=in_a.admin, plan=plan).pk == draft.pk
    assert draft.number == 2
    assert [i.text for i in draft.items.order_by("order")] == ["Purgar", "Presión"]
    assert draft.calendar_interval == 30


def test_publishing_new_frequency_recalculates_due_dates(in_a, today):
    assignment = services.assign_plan(plan=in_a.plan, asset=in_a.asset, starts_on=today)
    draft = services.create_draft(membership=in_a.admin, plan=in_a.plan)
    services.update_draft(
        membership=in_a.admin,
        version=draft,
        calendar_interval=60,
        meter_kind="",
        meter_interval=None,
        schedule_mode="fixed",
        tolerance_pct=5,
        estimated_minutes=90,
        instructions="Con el equipo detenido",
    )
    services.publish_draft(membership=in_a.admin, version=draft)
    assignment.refresh_from_db()
    # Sin ejecución previa: inicio implícito (hoy − 30) + 60 días.
    assert assignment.next_due_on == today + datetime.timedelta(days=30)
    versions = dict(in_a.plan.versions.values_list("number", "status"))
    assert versions == {1: PlanVersion.Status.SUPERSEDED, 2: PlanVersion.Status.PUBLISHED}


def test_published_version_cannot_be_edited_through_services(in_a):
    version = in_a.plan.current_version
    with pytest.raises(PlanVersionError):
        services.add_draft_item(membership=in_a.admin, version=version, text="x", item_type=ItemType.CHECK)
    with pytest.raises(PlanVersionError):
        services.update_draft(membership=in_a.admin, version=version, calendar_interval=7)
    with pytest.raises(PlanVersionError):
        services.discard_draft(membership=in_a.admin, version=version)


def test_items_move_edit_and_remove_renumber(in_a):
    draft = services.create_draft(membership=in_a.admin, plan=in_a.plan)
    first, second = draft.items.order_by("order")
    services.move_draft_item(membership=in_a.admin, item=second, direction="up")
    assert [i.text for i in draft.items.order_by("order")] == ["Presión", "Purgar"]
    services.update_draft_item(
        membership=in_a.admin,
        item=second,
        text="Presión de salida",
        item_type=ItemType.MEASUREMENT,
        unit="bar",
        min_value=Decimal("6"),
        max_value=Decimal("8"),
        required=False,
    )
    with pytest.raises(ValidationError):
        services.update_draft_item(
            membership=in_a.admin,
            item=second,
            text="x",
            item_type=ItemType.MEASUREMENT,
            min_value=Decimal("9"),
            max_value=Decimal("1"),
        )
    services.remove_draft_item(membership=in_a.admin, item=second)
    assert list(draft.items.values_list("text", "order")) == [("Purgar", 1)]
    # La versión publicada no cambió.
    assert in_a.plan.current_version.items.count() == 2


def test_resources_add_edit_remove_and_discard(in_a):
    draft = services.create_draft(membership=in_a.admin, plan=in_a.plan)
    resource = services.add_draft_resource(
        membership=in_a.admin, version=draft, kind="part", description="Filtro", quantity=Decimal("2")
    )
    services.update_draft_resource(
        membership=in_a.admin, resource=resource, kind="tool", description="Llave 13", quantity=1, unit="u"
    )
    with pytest.raises(ValidationError):
        services.add_draft_resource(
            membership=in_a.admin, version=draft, kind="part", description="x", quantity=0
        )
    services.remove_draft_resource(membership=in_a.admin, resource=resource)
    assert not draft.resources.exists()
    services.discard_draft(membership=in_a.admin, version=draft)
    assert not in_a.plan.versions.filter(status=PlanVersion.Status.DRAFT).exists()
    assert in_a.plan.current_version.number == 1


def test_batch_assignment_is_idempotent_and_warns_on_missing_meter(in_a):
    other = Asset.objects.create(code="CMP-02", name="Compresor 2", asset_class=in_a.asset.asset_class)
    assigned, warnings = services.assign_plan_to_assets(
        membership=in_a.admin, plan=in_a.plan, targets=[(in_a.asset, None), (other, None)]
    )
    assert (assigned, warnings) == (2, [])
    due = PlanAssignment.objects.get(asset=other).next_due_on
    assert services.assign_plan_to_assets(membership=in_a.admin, plan=in_a.plan, targets=[(other, None)]) == (
        0,
        [],
    )
    assert PlanAssignment.objects.get(asset=other).next_due_on == due

    usage = create_plan(code="U", name="250 h", meter_kind="hours", meter_interval=250, items=[{"text": "x"}])
    assigned, warnings = services.assign_plan_to_assets(
        membership=in_a.admin, plan=usage, targets=[(in_a.asset, None)]
    )
    assert assigned == 0 and "CMP-01" in warnings[0]


def test_usage_plan_uses_the_chosen_meter(in_a):
    Meter.objects.create(asset=in_a.asset, kind="hours", name="Motor", current_value=100)
    second = Meter.objects.create(asset=in_a.asset, kind="hours", name="Compresor", current_value=40)
    usage = create_plan(code="U", name="250 h", meter_kind="hours", meter_interval=250, items=[{"text": "x"}])
    services.assign_plan_to_assets(membership=in_a.admin, plan=usage, targets=[(in_a.asset, second)])
    assignment = PlanAssignment.objects.get(plan=usage)
    assert assignment.meter == second
    assert assignment.next_due_meter_value == 290


def test_assign_to_whole_class(in_a):
    Asset.objects.create(code="CMP-02", name="Compresor 2", asset_class=in_a.asset.asset_class)
    Asset.objects.create(
        code="CMP-03", name="Compresor 3", asset_class=in_a.asset.asset_class, status="retired"
    )
    other_class = AssetClass.objects.create(code="BMB", name="Bomba")
    Asset.objects.create(code="BMB-01", name="Bomba", asset_class=other_class)
    assigned, _warnings = services.assign_plan_to_class(membership=in_a.admin, plan=in_a.plan)
    assert assigned == 2
    assert set(PlanAssignment.objects.values_list("asset__code", flat=True)) == {"CMP-01", "CMP-02"}


def test_new_asset_receives_class_plans(in_a):
    asset = create_asset(
        membership=in_a.admin, code="CMP-09", name="Nuevo", asset_class=in_a.asset.asset_class
    )
    assert list(asset.plan_assignments.values_list("plan__code", flat=True)) == ["PM1"]


def test_new_asset_ignores_unpublished_and_inactive_class_plans(in_a):
    cls = in_a.asset.asset_class
    _new_plan(in_a.admin, code="D", asset_class=cls)  # solo borrador
    in_a.plan.is_active = False
    in_a.plan.save()
    asset = create_asset(membership=in_a.admin, code="CMP-09", name="Nuevo", asset_class=cls)
    assert not asset.plan_assignments.exists()


def test_deactivate_assignment_keeps_history(in_a):
    assignment = services.assign_plan(plan=in_a.plan, asset=in_a.asset)
    services.deactivate_assignment(membership=in_a.admin, assignment=assignment)
    services.deactivate_assignment(membership=in_a.admin, assignment=assignment)  # idempotente
    assignment.refresh_from_db()
    assert assignment.is_active is False
    assert AuditEvent.objects.filter(target_id=assignment.pk, action=AuditEvent.Action.UPDATE).count() == 1


# --- Vistas -------------------------------------------------------------------


def test_plan_list_filters(client, tenant_a):
    with tenant_context(tenant_a.org):
        create_plan(code="U", name="Horas", meter_kind="hours", meter_interval=250, items=[{"text": "x"}])
    login(client, tenant_a.admin)
    url = reverse("plan_list")
    names = lambda query: {p.name for p in client.get(url, query).context["page"].object_list}  # noqa: E731
    assert names({}) == {"Mensual", "Horas"}
    assert names({"trigger": "meter"}) == {"Horas"}
    assert names({"trigger": "calendar"}) == {"Mensual"}
    assert names({"q": "hor"}) == {"Horas"}
    assert names({"asset_class": str(tenant_a.plan.asset_class_id)}) == {"Mensual"}
    assert names({"active": "0"}) == set()


def test_full_draft_flow_through_the_screens(client, tenant_a):
    planner = _planner(tenant_a)
    login(client, planner)
    response = client.post(
        reverse("plan_create"),
        {
            "code": "LUB",
            "name": "Lubricación",
            "calendar_interval": "15",
            "schedule_mode": "floating",
            "tolerance_pct": "10",
            "estimated_minutes": "30",
        },
    )
    with tenant_context(tenant_a.org):
        plan = MaintenancePlan.objects.get(code="LUB")
    draft_url = reverse("plan_draft", args=[plan.pk])
    assert response.url == draft_url
    assert client.get(draft_url).status_code == 200
    client.post(
        draft_url,
        {"action": "item", "item-text": "Engrasar", "item-item_type": "check", "item-required": "on"},
    )
    client.post(
        draft_url,
        {
            "action": "resource",
            "resource-kind": "part",
            "resource-description": "Grasa",
            "resource-quantity": "1",
        },
    )
    confirm = client.get(reverse("plan_publish", args=[plan.pk]))
    assert "no se puede modificar" in confirm.content.decode()
    client.post(reverse("plan_publish", args=[plan.pk]), {"change_note": "Inicial"})
    with tenant_context(tenant_a.org):
        plan = MaintenancePlan.objects.get(pk=plan.pk)
        assert plan.current_version.number == 1
        assert plan.current_version.resources.count() == 1
    detail = client.get(reverse("plan_detail", args=[plan.pk]))
    assert detail.status_code == 200 and "Grasa" in detail.content.decode()


def test_header_validation_shows_errors(client, tenant_a):
    login(client, tenant_a.admin)
    with tenant_context(tenant_a.org):
        services.create_draft(membership=tenant_a.admin, plan=tenant_a.plan)
    url = reverse("plan_draft", args=[tenant_a.plan.pk])
    response = client.post(
        url,
        {
            "action": "header",
            "plan-code": "PM1",
            "plan-name": "Mensual",
            "plan-is_active": "on",
            "trigger-schedule_mode": "floating",
            "trigger-tolerance_pct": "10",
            "trigger-estimated_minutes": "60",
        },
    )
    assert response.status_code == 200
    assert "Define una frecuencia" in response.content.decode()


def test_picker_assigns_marked_assets(client, tenant_a):
    login(client, tenant_a.admin)
    url = reverse("plan_assign", args=[tenant_a.plan.pk])
    page = client.get(url)
    assert [a.code for a in page.context["page"].object_list] == ["CMP-01"]
    response = client.post(url, {"assets": [str(tenant_a.asset.pk)]})
    assert response.status_code == 302
    with tenant_context(tenant_a.org):
        assert PlanAssignment.objects.filter(asset=tenant_a.asset, is_active=True).exists()
    assert list(client.get(url).context["page"].object_list) == []


def test_asset_detail_offers_plan_assignment(client, tenant_a):
    login(client, tenant_a.admin)
    body = client.get(reverse("asset_detail", args=[tenant_a.asset.pk])).content.decode()
    assert reverse("asset_plan_assign", args=[tenant_a.asset.pk]) in body
    response = client.post(
        reverse("asset_plan_assign", args=[tenant_a.asset.pk]), {"plan": str(tenant_a.plan.pk)}
    )
    assert response.status_code == 302
    body = client.get(reverse("asset_detail", args=[tenant_a.asset.pk])).content.decode()
    assert reverse("plan_detail", args=[tenant_a.plan.pk]) in body


def test_preventive_work_order_shows_plan_resources_and_instructions(client, tenant_a, today):
    with tenant_context(tenant_a.org):
        draft = services.create_draft(membership=tenant_a.admin, plan=tenant_a.plan)
        services.update_draft(membership=tenant_a.admin, version=draft, instructions="Bloquear energía")
        services.add_draft_resource(
            membership=tenant_a.admin, version=draft, kind="part", description="Filtro de aire", quantity=1
        )
        services.publish_draft(membership=tenant_a.admin, version=draft)
        services.assign_plan(plan=tenant_a.plan, asset=tenant_a.asset, starts_on=today)
        (wo,) = generate_due_work_orders(today=today)
    login(client, tenant_a.tech)
    body = client.get(reverse("work_order_detail", args=[wo.pk])).content.decode()
    assert "Filtro de aire" in body and "Bloquear energía" in body
