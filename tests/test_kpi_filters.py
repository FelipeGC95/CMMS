"""KPI-01 con filtros del tablero: sitio (con descendientes), clase, criticidad, activo y
rango personalizado. Resultados conocidos sobre una ventana de 10 días (240 h)."""

import datetime

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.assets.models import Asset, AssetClass, AssetStatusPeriod
from apps.core.tenancy import tenant_context
from apps.organizations.models import Location
from apps.reporting.selectors import KpiFilters, dashboard_kpis
from apps.work.models import FailureEvent, WorkOrder
from apps.work.services import create_work_order
from tests.conftest import login

T0 = timezone.make_aware(datetime.datetime(2026, 9, 1))
NOW = T0 + datetime.timedelta(days=10)


def _at(days=0, hours=0):
    return T0 + datetime.timedelta(days=days, hours=hours)


def _periods(asset, *spans):
    for status, start, end in spans:
        AssetStatusPeriod.objects.create(asset=asset, status=status, started_at=start, ended_at=end)
    Asset.objects.filter(pk=asset.pk).update(status=spans[-1][0])


def _stop(asset, day, hours):
    """Detención total de `hours` horas el día `day`, con su falla."""
    _periods(
        asset,
        ("operational", _at(-30), _at(day)),
        ("stopped", _at(day), _at(day, hours)),
        ("operational", _at(day, hours), None),
    )
    FailureEvent.objects.create(
        asset=asset, impact="total_stop", started_at=_at(day), ended_at=_at(day, hours)
    )


def _preventive(asset, due_day, completed_day=None):
    wo = create_work_order(work_type="preventive", title="PM", asset=asset, due_on=_at(due_day).date())
    if completed_day is not None:
        WorkOrder.objects.filter(pk=wo.pk).update(status="completed", completed_at=_at(completed_day, 10))
    return wo


@pytest.fixture
def fleet(in_a):
    """Norte (con su área): N1 (compresor, A) detenido 12 h. Sur: S1 (bomba, C) detenido 6 h.
    CMP-01 (compresor, A, sin ubicación) sin detenciones. Preventivas: N1 a tiempo, S1 vencida."""
    north = Location.objects.create(code="N", name="Planta Norte")
    area = Location.objects.create(code="N-A", name="Envasado", kind="area", parent=north)
    south = Location.objects.create(code="S", name="Planta Sur")
    pumps = AssetClass.objects.create(code="BOM", name="Bomba")
    n1 = Asset.objects.create(
        code="N1", name="Compresor norte", asset_class=in_a.asset.asset_class, location=area, criticality="A"
    )
    s1 = Asset.objects.create(code="S1", name="Bomba sur", asset_class=pumps, location=south, criticality="C")
    _stop(n1, 2, 12)
    _stop(s1, 5, 6)
    _preventive(n1, 3, completed_day=3)
    _preventive(s1, 4)
    return {"north": north, "south": south, "pumps": pumps, "n1": n1, "s1": s1}


def _kpis(in_a, **filters):
    return dashboard_kpis(membership=in_a.admin, now=NOW, filters=KpiFilters(days=10, **filters))


def test_unfiltered_fleet(in_a, fleet):
    kpis = _kpis(in_a)
    calendar = 3 * 240
    assert round(kpis.availability_pct, 6) == round(100 * (1 - 18 / calendar), 6)
    assert (kpis.failures, kpis.mttr_hours, kpis.mtbf_hours) == (2, 9, (calendar - 18) / 2)
    assert kpis.preventive_compliance_pct == 50


def test_site_filter_includes_descendant_locations(in_a, fleet):
    kpis = _kpis(in_a, location=fleet["north"])
    assert kpis.availability_pct == 95  # 1 − 12/240
    assert (kpis.failures, kpis.mttr_hours, kpis.mtbf_hours) == (1, 12, 228)
    assert kpis.preventive_compliance_pct == 100


def test_asset_class_filter(in_a, fleet):
    kpis = _kpis(in_a, asset_class=fleet["pumps"])
    assert kpis.availability_pct == 97.5  # 1 − 6/240
    assert (kpis.failures, kpis.mttr_hours, kpis.mtbf_hours) == (1, 6, 234)
    assert kpis.preventive_compliance_pct == 0
    assert kpis.open_work_orders == 1


def test_criticality_filter(in_a, fleet):
    kpis = _kpis(in_a, criticality="A")  # CMP-01 y N1: 480 h calendario, 12 h detenido
    assert kpis.availability_pct == 97.5
    assert (kpis.failures, kpis.mtbf_hours) == (1, 468)


def test_single_asset_filter(in_a, fleet):
    kpis = _kpis(in_a, asset=in_a.asset)
    assert (kpis.availability_pct, kpis.failures, kpis.mttr_hours, kpis.mtbf_hours) == (100, 0, None, None)
    assert kpis.preventive_compliance_pct is None


def test_custom_range_crossing_retirement(in_a, fleet):
    """S1 de baja desde el día 6; rango del día 4 al 7 (96 h): solo 48 h en servicio, 6 h detenido."""
    s1 = fleet["s1"]
    AssetStatusPeriod.objects.filter(asset=s1, ended_at__isnull=True).update(ended_at=_at(6))
    _periods(s1, ("retired", _at(6), None))

    kpis = _kpis(in_a, asset_class=fleet["pumps"], date_from=_at(4).date(), date_to=_at(7).date())
    assert (kpis.period_start, kpis.period_end) == (_at(4).date(), _at(7).date())
    assert kpis.availability_pct == 87.5  # 1 − 6/48
    assert (kpis.failures, kpis.mttr_hours, kpis.mtbf_hours) == (1, 6, 42)
    assert kpis.preventive_compliance_pct == 0  # vence el día 4, dentro del rango


def test_custom_range_excludes_failures_outside(in_a, fleet):
    kpis = _kpis(in_a, location=fleet["north"], date_from=_at(5).date(), date_to=_at(9).date())
    assert (kpis.availability_pct, kpis.failures, kpis.mtbf_hours) == (100, 0, None)
    assert kpis.preventive_compliance_pct is None  # la preventiva de N1 vencía el día 3


def test_dashboard_view_applies_filters_and_shows_formulas(client, tenant_a):
    with tenant_context(tenant_a.org):
        north = Location.objects.create(code="N", name="Planta Norte")
    login(client, tenant_a.admin)
    response = client.get(reverse("dashboard"), {"period": "90", "location": north.pk, "criticality": "A"})
    assert response.status_code == 200
    body = response.content.decode()
    assert 'class="kpi__formula"' in body
    assert "EN 15341" in body
    assert response.context["active_filters"] == 3


def test_dashboard_view_reports_invalid_custom_range(client, tenant_a):
    login(client, tenant_a.admin)
    response = client.get(
        reverse("dashboard"), {"period": "custom", "date_from": "2026-09-10", "date_to": "2026-09-01"}
    )
    assert response.status_code == 200
    assert "date_to" in response.context["filter_form"].errors


def test_dashboard_unknown_asset_code_is_a_field_error(client, tenant_a):
    login(client, tenant_a.admin)
    response = client.get(reverse("dashboard"), {"asset": "NO-EXISTE"})
    assert response.status_code == 200
    assert "asset" in response.context["filter_form"].errors


def test_dates_alone_select_the_custom_range(client, tenant_a):
    login(client, tenant_a.admin)
    response = client.get(reverse("dashboard"), {"period": "30", "date_from": "2026-09-01"})
    assert response.context["kpis"].period_start == datetime.date(2026, 9, 1)
