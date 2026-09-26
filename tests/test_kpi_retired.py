"""KPI-01 con activos dados de baja: los períodos de baja no suman tiempo calendario
y las fallas iniciadas mientras el activo estaba de baja no cuentan.

Ventana de 10 días (240 h) que termina en `NOW`; resultados conocidos."""

import datetime

from django.urls import reverse

from apps.assets.models import Asset, AssetStatusPeriod
from apps.reporting.selectors import asset_kpis, dashboard_kpis
from apps.work.models import FailureEvent
from tests.conftest import login

T0 = datetime.datetime(2026, 9, 1, 8, 0, tzinfo=datetime.UTC)
NOW = T0 + datetime.timedelta(days=10)


def _at(days=0, hours=0):
    return T0 + datetime.timedelta(days=days, hours=hours)


def _periods(asset, *spans):
    """`spans`: (estado, inicio, término|None) consecutivos; deja `asset.status` en el último."""
    for status, start, end in spans:
        AssetStatusPeriod.objects.create(asset=asset, status=status, started_at=start, ended_at=end)
    Asset.objects.filter(pk=asset.pk).update(status=spans[-1][0])
    asset.refresh_from_db()


def _failure(asset, start, hours=None):
    end = start + datetime.timedelta(hours=hours) if hours is not None else None
    return FailureEvent.objects.create(asset=asset, impact="total_stop", started_at=start, ended_at=end)


def _kpis(in_a):
    return asset_kpis(membership=in_a.admin, asset=in_a.asset, days=10, now=NOW)


def test_retired_within_window_counts_only_time_in_service(in_a):
    """En servicio 6 días (144 h), detenido 12 h por una falla; de baja los últimos 4 días."""
    _periods(
        in_a.asset,
        ("operational", _at(-30), _at(2)),
        ("stopped", _at(2), _at(2, 12)),
        ("operational", _at(2, 12), _at(6)),
        ("retired", _at(6), None),
    )
    _failure(in_a.asset, _at(2), hours=12)

    kpis = _kpis(in_a)
    assert kpis.calendar_hours == 144
    assert round(kpis.availability_pct, 6) == round(100 * (1 - 12 / 144), 6)
    assert (kpis.failures, kpis.mttr_hours, kpis.mtbf_hours) == (1, 12, 144 - 12)


def test_retired_before_window_has_no_calendar_time(in_a):
    """De baja desde antes de la ventana: sin tiempo calendario, sin KPI (nunca negativos)."""
    _periods(in_a.asset, ("operational", _at(-60), _at(-5)), ("retired", _at(-5), None))
    _failure(in_a.asset, _at(3), hours=4)  # registrada con el activo de baja: no cuenta

    kpis = _kpis(in_a)
    assert kpis.calendar_hours == 0
    assert (kpis.availability_pct, kpis.mtbf_hours, kpis.mttr_hours, kpis.failures) == (None, None, None, 0)


def test_failures_while_retired_are_ignored(in_a):
    """De baja 2 días y reactivado: 192 h en servicio; solo cuenta la falla posterior."""
    _periods(
        in_a.asset,
        ("operational", _at(-30), _at(2)),
        ("retired", _at(2), _at(4)),
        ("operational", _at(4), _at(5)),
        ("stopped", _at(5), _at(5, 6)),
        ("operational", _at(5, 6), None),
    )
    _failure(in_a.asset, _at(3), hours=10)  # durante la baja
    _failure(in_a.asset, _at(5), hours=6)

    kpis = _kpis(in_a)
    assert kpis.calendar_hours == 192
    assert round(kpis.availability_pct, 6) == round(100 * (1 - 6 / 192), 6)
    assert (kpis.failures, kpis.mttr_hours, kpis.mtbf_hours) == (1, 6, 192 - 6)


def test_retired_asset_without_failures(in_a):
    _periods(in_a.asset, ("operational", _at(-30), _at(6)), ("retired", _at(6), None))

    kpis = _kpis(in_a)
    assert kpis.calendar_hours == 144
    assert (kpis.availability_pct, kpis.mtbf_hours, kpis.mttr_hours, kpis.failures) == (100, None, None, 0)


def test_retired_status_without_period_counts_as_retired_all_window(in_a):
    """Datos anteriores a los períodos de estado: el estado actual manda."""
    Asset.objects.filter(pk=in_a.asset.pk).update(status="retired")
    in_a.asset.refresh_from_db()
    _failure(in_a.asset, _at(3), hours=2)

    kpis = _kpis(in_a)
    assert (kpis.calendar_hours, kpis.availability_pct, kpis.mtbf_hours, kpis.failures) == (0, None, None, 0)


def test_dashboard_adds_retired_asset_time_in_service(in_a):
    """Flota: CMP-01 en servicio toda la ventana (240 h, detenido 6 h, 1 falla) y CMP-02
    dado de baja al día 6 (144 h, detenido 12 h, 1 falla antes y 1 durante la baja)."""
    other = Asset.objects.create(code="CMP-02", name="Compresor 2", asset_class=in_a.asset.asset_class)
    _periods(
        in_a.asset,
        ("operational", _at(-30), _at(1)),
        ("stopped", _at(1), _at(1, 6)),
        ("operational", _at(1, 6), None),
    )
    _failure(in_a.asset, _at(1), hours=6)
    _periods(
        other,
        ("operational", _at(-30), _at(2)),
        ("stopped", _at(2), _at(2, 12)),
        ("operational", _at(2, 12), _at(6)),
        ("retired", _at(6), None),
    )
    _failure(other, _at(2), hours=12)
    _failure(other, _at(8), hours=1)

    kpis = dashboard_kpis(membership=in_a.admin, days=10, now=NOW)
    calendar, down = 240 + 144, 6 + 12
    assert round(kpis.availability_pct, 6) == round(100 * (1 - down / calendar), 6)
    assert (kpis.failures, kpis.mttr_hours, kpis.mtbf_hours) == (2, 9, (calendar - down) / 2)
    assert kpis.stopped_assets == 0


def test_detail_explains_missing_kpis_for_retired_asset(client, in_a):
    Asset.objects.filter(pk=in_a.asset.pk).update(status="retired")
    login(client, in_a.admin)
    html = client.get(reverse("asset_detail", args=[in_a.asset.pk])).content.decode()
    assert "Sin tiempo en servicio en el período" in html
