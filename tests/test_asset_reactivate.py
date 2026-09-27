"""Reactivar un activo dado de baja (AST-04) y aviso de componentes al dar de baja."""

import datetime
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.urls import reverse
from django.utils import timezone

from apps.assets.models import Asset, AssetStatusPeriod, Meter
from apps.assets.services import decommission_asset, reactivate_asset, record_meter_reading
from apps.audit.models import AuditEvent
from apps.maintenance.models import ItemType, PlanAssignment
from apps.maintenance.services import assign_plan, create_plan
from tests.conftest import login


def _plan(code, **fields):
    return create_plan(
        code=code, name=f"Plan {code}", items=[{"text": "Revisar", "item_type": ItemType.CHECK}], **fields
    )


@pytest.fixture
def retired(in_a):
    """CMP-01 con tres planes (dos por calendario y uno por uso) y luego dado de baja."""
    asset = in_a.asset
    meter = Meter.objects.create(asset=asset, kind="hours")
    record_meter_reading(meter=meter, value=100, read_at=timezone.now() - datetime.timedelta(days=1))
    long_ago = timezone.localdate() - datetime.timedelta(days=200)
    calendar = assign_plan(plan=in_a.plan, asset=asset, starts_on=long_ago)
    usage = assign_plan(plan=_plan("PM-H", meter_kind="hours", meter_interval=Decimal("250")), asset=asset)
    other = assign_plan(plan=_plan("PM-W", calendar_interval=7), asset=asset, starts_on=long_ago)
    decommission_asset(membership=in_a.admin, asset=asset, reason="Vendido")
    return asset, meter, calendar, usage, other


def test_reactivate_opens_operational_period_and_reactivates_selected_plans(in_a, retired):
    asset, meter, calendar, usage, other = retired
    record_meter_reading(meter=meter, value=400)

    reactivate_asset(
        membership=in_a.admin, asset=asset, reason="Recomprado", assignment_ids=[calendar.pk, usage.pk]
    )

    asset.refresh_from_db()
    assert asset.status == "operational"
    periods = list(AssetStatusPeriod.objects.filter(asset=asset).order_by("started_at"))
    assert [(p.status, p.ended_at is None) for p in periods] == [("retired", False), ("operational", True)]
    assert periods[0].ended_at == periods[1].started_at
    assert (periods[1].reason, periods[1].changed_by) == ("Recomprado", in_a.admin.user)
    event = AuditEvent.objects.filter(target_id=str(asset.pk), action="transition").latest("created_at")
    assert event.changes["status"] == ["retired", "operational"]
    assert event.changes["reason"] == "Recomprado"

    # Vencimientos recalculados desde hoy (no desde la fecha vieja) y auditados.
    calendar.refresh_from_db()
    assert calendar.is_active and calendar.next_due_on == timezone.localdate() + datetime.timedelta(days=30)
    usage.refresh_from_db()
    assert usage.is_active and usage.next_due_meter_value == Decimal("650")
    for assignment in (calendar, usage):
        changes = AuditEvent.objects.filter(target_id=str(assignment.pk)).latest("created_at").changes
        assert changes["is_active"] == [False, True]
    # El no marcado sigue desactivado y sin cambios.
    before = other.next_due_on
    other.refresh_from_db()
    assert other.is_active is False and other.next_due_on == before


def test_reactivate_without_plans_keeps_all_assignments_inactive(in_a, retired):
    asset = retired[0]
    reactivate_asset(membership=in_a.admin, asset=asset, reason="Vuelve")
    assert not PlanAssignment.objects.filter(asset=asset, is_active=True).exists()


@pytest.mark.parametrize("reason", ["", "   "])
def test_reactivate_requires_reason(in_a, retired, reason):
    asset = retired[0]
    with pytest.raises(ValidationError):
        reactivate_asset(membership=in_a.admin, asset=asset, reason=reason)
    asset.refresh_from_db()
    assert asset.status == "retired"


def test_reactivate_rejects_active_asset_and_foreign_assignments(in_a, retired):
    asset = retired[0]
    other_asset = Asset.objects.create(code="CMP-02", name="Compresor 2", asset_class=asset.asset_class)
    foreign = assign_plan(plan=in_a.plan, asset=other_asset)
    with pytest.raises(ValidationError):
        reactivate_asset(membership=in_a.admin, asset=other_asset, reason="x")
    with pytest.raises(ValidationError):
        reactivate_asset(membership=in_a.admin, asset=asset, reason="x", assignment_ids=[foreign.pk])
    asset.refresh_from_db()
    assert asset.status == "retired"


def test_reactivate_view_lists_plans_unchecked_and_reactivates(client, in_a, retired):
    asset, _meter, calendar, _usage, other = retired
    login(client, in_a.admin)
    detail = client.get(reverse("asset_detail", args=[asset.pk])).content.decode()
    url = reverse("asset_reactivate", args=[asset.pk])
    assert url in detail

    page = client.get(url)
    html = page.content.decode()
    assert page.status_code == 200
    assert calendar.plan.name in html and other.plan.name in html
    assert f'value="{calendar.pk}"' in html and "checked" not in html
    assert client.post(url, {"reason": ""}).status_code == 200  # motivo obligatorio
    response = client.post(url, {"reason": "Reparado", "assignments": [str(calendar.pk)]})
    assert response.status_code == 302
    asset.refresh_from_db()
    calendar.refresh_from_db()
    other.refresh_from_db()
    assert asset.status == "operational" and calendar.is_active and not other.is_active
    assert client.get(url).status_code == 302  # ya operativo: vuelve a la ficha
    assert url not in client.get(reverse("asset_detail", args=[asset.pk])).content.decode()


def test_decommission_page_warns_about_active_components(client, in_a):
    parent = in_a.asset
    motor = Asset.objects.create(code="MOT-1", name="Motor", asset_class=parent.asset_class, parent=parent)
    old = Asset.objects.create(
        code="VAL-1", name="Válvula", asset_class=parent.asset_class, parent=parent, status="retired"
    )
    login(client, in_a.admin)
    url = reverse("asset_decommission", args=[parent.pk])
    response = client.get(url)
    html = response.content.decode()
    assert list(response.context["active_children"]) == [motor]
    assert "siguen operativos" in html and motor.code in html and old.code not in html

    client.post(url, {"reason": "Reemplazado"})
    motor.refresh_from_db()
    assert motor.status == "operational"  # sin cascada


def test_decommission_page_without_components_has_no_warning(client, in_a):
    login(client, in_a.admin)
    html = client.get(reverse("asset_decommission", args=[in_a.asset.pk])).content.decode()
    assert "siguen operativos" not in html
