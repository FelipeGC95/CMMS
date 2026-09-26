"""Estados, detenciones y baja de activos (AST-04) e "incluir componentes" (AST-03 CA2)."""

import datetime
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.urls import reverse
from django.utils import timezone

from apps.assets.models import Asset, AssetStatusPeriod, Meter
from apps.assets.selectors import asset_scope_ids
from apps.assets.services import (
    change_asset_status,
    decommission_asset,
    record_meter_reading,
    set_asset_status,
)
from apps.audit.models import AuditEvent
from apps.maintenance.models import ItemType
from apps.maintenance.services import assign_plan, create_plan, generate_due_work_orders
from apps.reporting.selectors import asset_kpis
from apps.work.models import FailureEvent, WorkOrder
from apps.work.services import create_work_order
from tests.conftest import login

T0 = datetime.datetime(2026, 9, 1, 8, 0, tzinfo=datetime.UTC)


def _open_periods(asset):
    return AssetStatusPeriod.objects.filter(asset=asset, ended_at__isnull=True)


def _child(parent, code, **kwargs):
    return Asset.objects.create(code=code, name=code, asset_class=parent.asset_class, parent=parent, **kwargs)


# --- Períodos de estado (AST-04 CA2) ------------------------------------------------


def test_change_status_links_work_order_and_failure(in_a):
    asset = in_a.asset
    wo = create_work_order(work_type="corrective", title="Falla", asset=asset)
    failure = FailureEvent.objects.create(asset=asset, work_order=wo, impact="total_stop", started_at=T0)
    change_asset_status(
        asset=asset, status="stopped", reason=wo.code, at=T0, work_order=wo, failure_event=failure
    )

    period = _open_periods(asset).get()
    assert (period.status, period.work_order, period.failure_event) == ("stopped", wo, failure)
    event = AuditEvent.objects.filter(target_id=str(asset.pk)).latest("created_at")
    assert event.changes["work_order"] == str(wo.pk)
    assert event.changes["failure_event"] == str(failure.pk)


def test_manual_status_change_closes_and_opens_periods(in_a):
    asset = in_a.asset
    set_asset_status(membership=in_a.admin, asset=asset, status="stopped", reason="Sin energía")
    first = _open_periods(asset).get()
    set_asset_status(membership=in_a.admin, asset=asset, status="in_maintenance", reason="Revisión")
    set_asset_status(membership=in_a.admin, asset=asset, status="operational", reason="Listo")

    asset.refresh_from_db()
    assert asset.status == "operational"
    first.refresh_from_db()
    assert first.ended_at is not None and first.reason == "Sin energía"
    assert first.changed_by == in_a.admin.user
    periods = list(AssetStatusPeriod.objects.filter(asset=asset).order_by("started_at"))
    assert [p.status for p in periods] == ["stopped", "in_maintenance", "operational"]
    assert [p.ended_at is None for p in periods] == [False, False, True]
    # Cada período cierra exactamente cuando empieza el siguiente: sin huecos.
    assert all(a.ended_at == b.started_at for a, b in zip(periods, periods[1:], strict=False))
    assert AuditEvent.objects.filter(target_id=str(asset.pk), action="transition").count() == 3


@pytest.mark.parametrize(
    ("status", "reason", "field"),
    [
        ("stopped", "  ", "reason"),
        ("retired", "x", "status"),
        ("operational", "x", "status"),
        ("??", "x", "status"),
    ],
)
def test_manual_status_change_validates(in_a, status, reason, field):
    with pytest.raises(ValidationError) as error:
        set_asset_status(membership=in_a.admin, asset=in_a.asset, status=status, reason=reason)
    assert field in error.value.message_dict
    assert not AssetStatusPeriod.objects.filter(asset=in_a.asset).exists()


# --- Baja (AST-04 CA3) --------------------------------------------------------------


@pytest.fixture
def meter_plan(in_a):
    return create_plan(
        code="PM-H",
        name="Cada 250 h",
        meter_kind="hours",
        meter_interval=Decimal("250"),
        items=[{"text": "Cambiar aceite", "item_type": ItemType.CHECK}],
    )


def test_decommission_deactivates_assignments_and_keeps_open_work_orders(in_a, today, meter_plan):
    asset = in_a.asset
    meter = Meter.objects.create(asset=asset, kind="hours")
    record_meter_reading(meter=meter, value=100, read_at=timezone.now() - datetime.timedelta(days=1))
    calendar = assign_plan(plan=in_a.plan, asset=asset, starts_on=today)
    usage = assign_plan(plan=meter_plan, asset=asset)
    open_wo = create_work_order(work_type="corrective", title="Abierta", asset=asset)

    decommission_asset(membership=in_a.admin, asset=asset, reason="Vendido")

    asset.refresh_from_db()
    assert asset.status == "retired" and asset.is_active  # sin borrado: sigue en la base
    assert _open_periods(asset).get().reason == "Vendido"
    for assignment in (calendar, usage):
        assignment.refresh_from_db()
        assert assignment.is_active is False
        assert AuditEvent.objects.filter(target_id=str(assignment.pk), action="update").exists()
    open_wo.refresh_from_db()
    assert open_wo.status == WorkOrder.Status.PLANNED

    # No genera preventivos ni por fecha ni por uso.
    assert generate_due_work_orders(today=today, horizon_days=60) == []
    record_meter_reading(meter=meter, value=900)
    assert not WorkOrder.objects.filter(work_type="preventive").exists()
    with pytest.raises(ValidationError):
        assign_plan(plan=in_a.plan, asset=asset)
    with pytest.raises(ValidationError):
        decommission_asset(membership=in_a.admin, asset=asset, reason="Otra vez")
    with pytest.raises(ValidationError):
        set_asset_status(membership=in_a.admin, asset=asset, status="operational", reason="Volvió")


def test_retired_status_alone_blocks_generation(in_a, today):
    """Aunque una asignación quedara activa, un activo de baja no genera OT."""
    assign_plan(plan=in_a.plan, asset=in_a.asset, starts_on=today)
    change_asset_status(asset=in_a.asset, status="retired", reason="x")
    assert generate_due_work_orders(today=today) == []


def test_decommission_requires_reason(in_a):
    with pytest.raises(ValidationError):
        decommission_asset(membership=in_a.admin, asset=in_a.asset, reason="")
    in_a.asset.refresh_from_db()
    assert in_a.asset.status == "operational"


def test_decommission_view_confirms_then_retires(client, in_a, today):
    assign_plan(plan=in_a.plan, asset=in_a.asset, starts_on=today)
    wo = create_work_order(work_type="corrective", title="Pendiente", asset=in_a.asset)
    login(client, in_a.admin)
    url = reverse("asset_decommission", args=[in_a.asset.pk])
    page = client.get(url)
    assert page.status_code == 200
    html = page.content.decode()
    assert in_a.plan.name in html and wo.code in html
    assert client.post(url, {"reason": ""}).status_code == 200  # motivo obligatorio
    response = client.post(url, {"reason": "Reemplazado"})
    assert response.status_code == 302
    in_a.asset.refresh_from_db()
    assert in_a.asset.status == "retired"
    detail = client.get(reverse("asset_detail", args=[in_a.asset.pk])).content.decode()
    assert url not in detail and reverse("asset_status", args=[in_a.asset.pk]) not in detail
    assert client.get(url).status_code == 302  # ya de baja: vuelve a la ficha


def test_status_view_changes_status(client, in_a):
    login(client, in_a.admin)
    url = reverse("asset_status", args=[in_a.asset.pk])
    form = client.get(url).context["form"]
    assert [value for value, _ in form.fields["status"].choices] == ["stopped", "in_maintenance"]
    assert client.post(url, {"status": "stopped", "reason": ""}).status_code == 200
    response = client.post(url, {"status": "stopped", "reason": "Atasco"})
    assert response.status_code == 302
    in_a.asset.refresh_from_db()
    assert in_a.asset.status == "stopped"


# --- Incluir componentes (AST-03 CA2) -----------------------------------------------


@pytest.fixture
def tree(in_a):
    """CMP-01 → SYS (sistema) → MOT (componente); CMP-01 → VAL."""
    system = _child(in_a.asset, "SYS")
    motor = _child(system, "MOT")
    valve = _child(in_a.asset, "VAL")
    return in_a.asset, system, motor, valve


def test_scope_ids_include_all_descendants(in_a, tree):
    root, system, motor, valve = tree
    assert asset_scope_ids(membership=in_a.admin, asset=root) == [root.pk]
    ids = asset_scope_ids(membership=in_a.admin, asset=root, include_components=True)
    assert ids[0] == root.pk and set(ids) == {root.pk, system.pk, motor.pk, valve.pk}
    assert set(asset_scope_ids(membership=in_a.admin, asset=system, include_components=True)) == {
        system.pk,
        motor.pk,
    }


def test_asset_kpis_with_known_values_including_components(in_a, tree):
    """Ventana de 10 días; raíz detenida 12 h (1 falla, reparada en 12 h); motor detenido 24 h
    (1 falla, reparada en 24 h); una falla parcial en la válvula no cuenta."""
    root, system, motor, valve = tree
    now = T0 + datetime.timedelta(days=10)
    for asset, hours in ((root, 12), (motor, 24)):
        start = T0 + datetime.timedelta(days=2)
        end = start + datetime.timedelta(hours=hours)
        AssetStatusPeriod.objects.create(asset=asset, status="stopped", started_at=start, ended_at=end)
        FailureEvent.objects.create(asset=asset, impact="total_stop", started_at=start, ended_at=end)
    FailureEvent.objects.create(asset=valve, impact="partial", started_at=T0 + datetime.timedelta(days=3))

    alone = asset_kpis(membership=in_a.admin, asset=root, days=10, now=now)
    assert round(alone.availability_pct, 6) == round(100 * (1 - 12 / 240), 6)
    assert (alone.failures, alone.mttr_hours, alone.mtbf_hours) == (1, 12, 240 - 12)

    total = asset_kpis(membership=in_a.admin, asset=root, include_components=True, days=10, now=now)
    # 4 activos × 240 h = 960 h calendario; 36 h detenidas.
    assert round(total.availability_pct, 6) == round(100 * (1 - 36 / 960), 6)
    assert (total.failures, total.mttr_hours, total.mtbf_hours) == (2, 18, (960 - 36) / 2)


def test_detail_with_components_shows_component_history(client, in_a, tree):
    root, system, motor, valve = tree
    FailureEvent.objects.create(
        asset=motor,
        impact="total_stop",
        description="Rodamiento",
        started_at=timezone.now() - datetime.timedelta(days=1),
    )
    change_asset_status(asset=motor, status="stopped", reason="Rodamiento trabado")
    login(client, in_a.admin)
    url = reverse("asset_detail", args=[root.pk])
    alone = client.get(url)
    assert "Rodamiento trabado" not in alone.content.decode()
    assert alone.context["include_components"] is False
    included = client.get(url, {"components": "1"})
    html = included.content.decode()
    assert included.context["include_components"] is True
    assert "Rodamiento trabado" in html and "Rodamiento" in html and "MOT" in html
    assert included.context["kpis"].failures == 1
