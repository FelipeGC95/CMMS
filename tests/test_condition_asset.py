"""Condición en la ficha del activo (C4): variables (CBM-01 CA3/CA5), lecturas manuales y por CSV
(CBM-02 CA1/CA2) y la sección "Condición" con estado y tendencia SVG (CBM-07)."""

import datetime
import io
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.urls import reverse
from django.utils import timezone

from apps.assets.forms import MeterReadingForm
from apps.assets.models import AssetClass, Meter, MeterKind, MeterReading
from apps.assets.reading_imports import (
    apply_reading_import,
    build_reading_preview,
    parse_csv,
    parse_kind,
    reading_client_uuid,
)
from apps.assets.services import (
    MeterReadingError,
    create_variable,
    record_meter_reading,
    set_variable_active,
    update_variable,
)
from apps.audit.models import AuditEvent
from apps.condition import services as condition
from apps.condition.charts import TrendPoint, build_trend, trend_scale
from apps.condition.selectors import TABLE_LIMIT, VariableStatus, asset_condition
from apps.core.tenancy import tenant_context
from tests.condition_utils import variable
from tests.conftest import login

NOW = datetime.datetime(2026, 10, 12, 12, 0, tzinfo=datetime.UTC)


def _csv(text):
    file = io.BytesIO(text.encode("utf-8-sig"))
    file.name = "lecturas.csv"
    file.size = len(file.getvalue())
    return file


def _at(days_ago, now=NOW):
    return now - datetime.timedelta(days=days_ago)


# --- Servicios: alta, edición y desactivación (CBM-01 CA3/CA5) -------------------------


def test_create_variable_uses_default_unit_and_is_never_cumulative(in_a):
    meter = create_variable(membership=in_a.admin, asset=in_a.asset, kind=MeterKind.VIBRATION, name="Motor")
    assert (meter.unit, meter.is_cumulative, meter.is_active) == ("mm/s", False, True)
    custom = create_variable(
        membership=in_a.admin, asset=in_a.asset, kind=MeterKind.TEMPERATURE, name="Aceite", unit="°F"
    )
    assert custom.unit == "°F"
    event = AuditEvent.objects.filter(target_id=str(meter.pk), action=AuditEvent.Action.CREATE).get()
    assert event.changes["kind"] == "vibration"


def test_create_variable_rejects_usage_kinds_and_unnamed_free_variable(in_a):
    with pytest.raises(ValidationError) as error:
        create_variable(membership=in_a.admin, asset=in_a.asset, kind=MeterKind.HOURS)
    assert "kind" in error.value.message_dict
    with pytest.raises(ValidationError) as error:
        create_variable(membership=in_a.admin, asset=in_a.asset, kind=MeterKind.VARIABLE, name="  ")
    assert "name" in error.value.message_dict
    meter = create_variable(
        membership=in_a.admin, asset=in_a.asset, kind=MeterKind.VARIABLE, name="Nivel de aceite", unit="%"
    )
    assert (meter.unit, meter.is_cumulative) == ("%", False)


def test_variable_key_is_unique_per_asset_including_deactivated(in_a):
    meter = create_variable(membership=in_a.admin, asset=in_a.asset, kind=MeterKind.TEMPERATURE, name="Motor")
    with pytest.raises(ValidationError) as error:
        create_variable(membership=in_a.admin, asset=in_a.asset, kind=MeterKind.TEMPERATURE, name="motor")
    assert "ya tiene un medidor" in error.value.message_dict["name"][0]
    # Otro tipo con el mismo nombre sí vale.
    create_variable(membership=in_a.admin, asset=in_a.asset, kind=MeterKind.VIBRATION, name="Motor")
    set_variable_active(membership=in_a.admin, meter=meter, is_active=False)
    with pytest.raises(ValidationError) as error:
        create_variable(membership=in_a.admin, asset=in_a.asset, kind=MeterKind.TEMPERATURE, name="Motor")
    assert "reactívala" in error.value.message_dict["name"][0]


def test_update_variable_changes_name_and_unit_with_audit_and_keeps_kind(in_a):
    meter = create_variable(membership=in_a.admin, asset=in_a.asset, kind=MeterKind.PRESSURE, name="Entrada")
    other = create_variable(membership=in_a.admin, asset=in_a.asset, kind=MeterKind.PRESSURE, name="Salida")
    update_variable(membership=in_a.admin, meter=meter, name="Succión", unit="psi")
    meter.refresh_from_db()
    assert (meter.kind, meter.name, meter.unit) == (MeterKind.PRESSURE, "Succión", "psi")
    event = AuditEvent.objects.filter(target_id=str(meter.pk), action=AuditEvent.Action.UPDATE).get()
    assert event.changes["name"] == ["Entrada", "Succión"]
    with pytest.raises(ValidationError) as error:
        update_variable(membership=in_a.admin, meter=meter, name="Salida", unit="bar")
    assert "name" in error.value.message_dict
    # Sin cambios no audita de nuevo.
    update_variable(membership=in_a.admin, meter=other, name="Salida", unit="bar")
    assert not AuditEvent.objects.filter(target_id=str(other.pk), action=AuditEvent.Action.UPDATE).exists()


def test_update_rejects_usage_meters(in_a):
    hours = Meter.objects.create(asset=in_a.asset, kind=MeterKind.HOURS)
    with pytest.raises(ValidationError):
        update_variable(membership=in_a.admin, meter=hours, name="x", unit="h")
    with pytest.raises(ValidationError):
        set_variable_active(membership=in_a.admin, meter=hours, is_active=False)


def test_deactivated_variable_keeps_history_and_alerts_and_rejects_readings(in_a):
    meter = variable(in_a.asset)
    condition.create_rule(membership=in_a.admin, meter=meter, operator="gt", threshold="80")
    record_meter_reading(meter=meter, value="85", read_at=_at(2))
    set_variable_active(membership=in_a.admin, meter=meter, is_active=False)
    meter.refresh_from_db()
    assert not meter.is_active
    assert meter.readings.count() == 1
    assert meter.condition_alerts.count() == 1
    with pytest.raises(MeterReadingError):
        record_meter_reading(meter=meter, value="70", read_at=_at(1))
    event = AuditEvent.objects.filter(target_id=str(meter.pk), action=AuditEvent.Action.UPDATE).get()
    assert event.changes == {"is_active": [True, False]}
    set_variable_active(membership=in_a.admin, meter=meter, is_active=True)
    meter.refresh_from_db()
    record_meter_reading(meter=meter, value="70", read_at=_at(1))
    assert meter.readings.count() == 2


# --- Lectura manual (CBM-02 CA1) --------------------------------------------------------


def test_reading_form_accepts_negatives_only_for_variables(in_a):
    temperature = variable(in_a.asset)
    hours = Meter.objects.create(asset=in_a.asset, kind=MeterKind.HOURS)
    assert MeterReadingForm({"value": "-12.5"}, meter=temperature).is_valid()
    assert "is_reset" not in MeterReadingForm(meter=temperature).fields
    form = MeterReadingForm({"value": "-1"}, meter=hours)
    assert not form.is_valid()
    assert "negativa" in form.errors["value"][0]
    assert MeterReadingForm({"value": "5"}, meter=hours).is_valid()


def test_manual_reading_of_a_variable_below_zero_from_the_asset(client, tenant_a):
    with tenant_context(tenant_a.org):
        meter = variable(tenant_a.asset)
    login(client, tenant_a.tech)
    url = reverse("meter_reading", args=[tenant_a.asset.pk, meter.pk])
    response = client.post(url, {f"{meter.pk}-value": "-18.5"})
    assert response.status_code == 302
    assert response["Location"].endswith(f"#meter-{meter.pk}")
    with tenant_context(tenant_a.org):
        meter.refresh_from_db()
        assert meter.current_value == Decimal("-18.5")
        assert meter.readings.get().source == MeterReading.Source.MANUAL


def test_manual_negative_reading_of_a_usage_meter_is_rejected(client, tenant_a):
    with tenant_context(tenant_a.org):
        meter = Meter.objects.create(asset=tenant_a.asset, kind=MeterKind.HOURS)
    login(client, tenant_a.tech)
    client.post(reverse("meter_reading", args=[tenant_a.asset.pk, meter.pk]), {f"{meter.pk}-value": "-1"})
    with tenant_context(tenant_a.org):
        assert not meter.readings.exists()


def test_manual_reading_is_idempotent_by_client_uuid(client, tenant_a):
    with tenant_context(tenant_a.org):
        meter = variable(tenant_a.asset)
    login(client, tenant_a.tech)
    data = {f"{meter.pk}-value": "40", f"{meter.pk}-client_uuid": "0190a3c4-0000-7000-8000-000000000001"}
    url = reverse("meter_reading", args=[tenant_a.asset.pk, meter.pk])
    client.post(url, data)
    client.post(url, data)
    with tenant_context(tenant_a.org):
        assert meter.readings.count() == 1


def test_deactivated_variable_does_not_accept_manual_readings(client, tenant_a):
    with tenant_context(tenant_a.org):
        meter = variable(tenant_a.asset)
        set_variable_active(membership=tenant_a.admin, meter=meter, is_active=False)
    login(client, tenant_a.tech)
    url = reverse("meter_reading", args=[tenant_a.asset.pk, meter.pk])
    assert client.post(url, {f"{meter.pk}-value": "40"}).status_code == 404


# --- CSV con tipo y nombre (CBM-02 CA2) -------------------------------------------------


def test_parse_kind_accepts_codes_labels_and_spanish_words():
    assert parse_kind("temperature") == MeterKind.TEMPERATURE
    assert parse_kind("Temperatura") == MeterKind.TEMPERATURE
    assert parse_kind("Temperatura (°C)") == MeterKind.TEMPERATURE
    assert parse_kind("vibración") == MeterKind.VIBRATION
    assert parse_kind("Presión") == MeterKind.PRESSURE
    assert parse_kind("horómetro") == MeterKind.HOURS
    assert parse_kind("otra") == MeterKind.VARIABLE
    assert parse_kind("") is None
    assert parse_kind("humedad") is None


@pytest.fixture
def several(in_a):
    """Un activo con horómetro y tres variables (dos del mismo tipo)."""
    return {
        "hours": Meter.objects.create(asset=in_a.asset, kind=MeterKind.HOURS),
        "bearing": variable(in_a.asset, name="Rodamiento"),
        "oil": variable(in_a.asset, name="Aceite"),
        "vibration": variable(in_a.asset, kind=MeterKind.VIBRATION, name=""),
    }


def test_csv_type_and_name_pick_the_variable(in_a, several):
    raw = parse_csv(
        _csv(
            "activo;tipo;nombre;valor;fecha\n"
            "CMP-01;temperatura;Rodamiento;-3,5;2026-09-20 08:00\n"
            "CMP-01;Temperatura;aceite;60;2026-09-20 08:00\n"
            "CMP-01;vibracion;;2,8;2026-09-20 08:00\n"
            "CMP-01;horas;;120;2026-09-20 08:00\n"
        )
    )
    preview = build_reading_preview(raw, membership=in_a.admin)
    assert [r.errors for r in preview.rows] == [[], [], [], []]
    assert [r.meter_id for r in preview.rows] == [
        str(several[k].pk) for k in ("bearing", "oil", "vibration", "hours")
    ]
    result = apply_reading_import(
        preview.to_session()["rows"], membership=in_a.admin, source=MeterReading.Source.IMPORT
    )
    assert result.created == 4
    several["bearing"].refresh_from_db()
    assert several["bearing"].current_value == Decimal("-3.5")


def test_csv_reports_ambiguity_unknown_type_and_missing_variable(in_a, several):
    raw = parse_csv(
        _csv(
            "asset_code,tipo,nombre,value,read_at\n"
            "CMP-01,,,5,2026-09-20\n"  # varios medidores y sin tipo ni nombre
            "CMP-01,temperatura,,5,2026-09-20\n"  # dos temperaturas: falta el nombre
            "CMP-01,humedad,,5,2026-09-20\n"  # tipo desconocido
            "CMP-01,presion,,5,2026-09-20\n"  # el activo no tiene presión
            "CMP-01,horas,,-5,2026-09-20\n"  # un medidor de uso no admite negativos
        )
    )
    errors = [r.errors for r in build_reading_preview(raw, membership=in_a.admin).rows]
    assert "indica el tipo y el nombre" in errors[0][0]
    assert "indica también el nombre" in errors[1][0]
    assert "Tipo de medidor desconocido" in errors[2][0]
    assert "no tiene un medidor con ese tipo y nombre" in errors[3][0]
    assert any("negativa" in e for e in errors[4])


def test_csv_meter_id_must_match_type_and_name(in_a, several):
    raw = [
        {
            "meter": str(several["bearing"].pk),
            "kind": "vibracion",
            "value": "1",
            "read_at": "2026-09-20",
            "_row": 2,
        },
        {
            "meter": str(several["bearing"].pk),
            "name": "Rodamiento",
            "value": "1",
            "read_at": "2026-09-20",
            "_row": 3,
        },
    ]
    rows = build_reading_preview(raw, membership=in_a.admin).rows
    assert "no coincide" in rows[0].errors[0]
    assert rows[1].errors == []


def test_old_csv_without_type_still_works_with_a_single_meter(in_a):
    meter = Meter.objects.create(asset=in_a.asset, kind=MeterKind.HOURS)
    raw = parse_csv(_csv("asset_code,value,read_at\nCMP-01,100,2026-09-20 08:00\n"))
    preview = build_reading_preview(raw, membership=in_a.admin)
    assert preview.rows[0].meter_id == str(meter.pk)


def test_csv_import_is_idempotent_with_a_stable_client_uuid(in_a, several):
    rows = [
        {
            "asset_code": "CMP-01",
            "kind": "vibration",
            "value": "2,5",
            "read_at": "2026-09-20 08:00",
            "_row": 2,
        }
    ]
    first = build_reading_preview(rows, membership=in_a.admin).to_session()["rows"]
    result = apply_reading_import(first, membership=in_a.admin, source=MeterReading.Source.IMPORT)
    reading = several["vibration"].readings.get()
    assert reading.client_uuid == reading_client_uuid(several["vibration"].pk, reading.read_at, reading.value)
    # Reenviar la misma vista previa (p. ej. doble confirmación) no duplica.
    again = apply_reading_import(first, membership=in_a.admin, source=MeterReading.Source.IMPORT)
    assert (result.created, again.created, again.duplicates) == (1, 0, 1)
    assert several["vibration"].readings.count() == 1
    assert build_reading_preview(rows, membership=in_a.admin).rows[0].action == "duplicate"


def test_csv_import_screen_shows_variable_rows(client, tenant_a):
    with tenant_context(tenant_a.org):
        variable(tenant_a.asset, name="Rodamiento")
        variable(tenant_a.asset, kind=MeterKind.VIBRATION, name="")
    login(client, tenant_a.admin)
    assert "tipo" in client.get(reverse("reading_import")).content.decode()
    body = (
        "asset_code;tipo;nombre;value;read_at\n"
        "CMP-01;temperatura;Rodamiento;-2;2026-09-20 08:00\n"
        "CMP-01;;;1;2026-09-20\n"
    )
    content = client.post(reverse("reading_import"), {"file": _csv(body)}).content.decode()
    assert "Rodamiento (°C)" in content
    assert "indica el tipo y el nombre" in content


# --- Selector de la sección "Condición" (CBM-07) ---------------------------------------


@pytest.fixture
def scenario(in_a):
    """Temperatura con regla directa > 80 y regla por clase fuera de 40–90; vibración sin reglas;
    presión desactivada. Lecturas conocidas dentro y fuera de la ventana de 90 días."""
    bearing = variable(in_a.asset, name="Rodamiento")
    vibration = variable(in_a.asset, kind=MeterKind.VIBRATION, name="")
    pressure = variable(in_a.asset, kind=MeterKind.PRESSURE, name="")
    condition.create_rule(membership=in_a.admin, meter=bearing, operator="gt", threshold="80")
    condition.create_rule(
        membership=in_a.admin,
        asset_class=in_a.asset.asset_class,
        meter_kind=MeterKind.TEMPERATURE,
        meter_name="Rodamiento",
        operator="outside",
        min_value="40",
        max_value="90",
    )
    # Regla de otra clase con la misma clave: no aplica.
    other_class = AssetClass.objects.create(code="BMB", name="Bomba")
    condition.create_rule(
        membership=in_a.admin,
        asset_class=other_class,
        meter_kind=MeterKind.TEMPERATURE,
        meter_name="Rodamiento",
        operator="gt",
        threshold="10",
    )
    record_meter_reading(meter=bearing, value="70", read_at=_at(120))  # fuera de la ventana
    record_meter_reading(meter=bearing, value="60", read_at=_at(30))
    record_meter_reading(meter=bearing, value="85", read_at=_at(2))  # > 80: alarma
    record_meter_reading(meter=vibration, value="3.2", read_at=_at(5))
    record_meter_reading(meter=pressure, value="6", read_at=_at(4))
    set_variable_active(membership=in_a.admin, meter=pressure, is_active=False)
    return bearing, vibration, pressure


def test_asset_condition_known_result(in_a, scenario):
    bearing, vibration, pressure = scenario
    rows = asset_condition(membership=in_a.admin, asset=in_a.asset, now=NOW)
    by_meter = {row.meter.pk: row for row in rows}
    assert [row.meter.pk for row in rows][-1] == pressure.pk  # desactivadas al final
    temp = by_meter[bearing.pk]
    assert temp.status == VariableStatus.ALARM
    assert [p.value for p in temp.points] == [Decimal("60"), Decimal("85")]
    assert [p.out_of_range for p in temp.points] == [False, True]
    assert sorted(temp.thresholds) == [Decimal("40"), Decimal("80"), Decimal("90")]  # por activo y por clase
    assert {r.operator for r in temp.rules} == {"gt", "outside"}
    assert temp.active_alerts == 1
    assert temp.since == NOW - datetime.timedelta(days=90) and temp.until == NOW
    assert [p.value for p in temp.recent] == [Decimal("85"), Decimal("60")]
    assert by_meter[vibration.pk].status == VariableStatus.NO_RULES
    assert by_meter[vibration.pk].thresholds == []
    assert by_meter[pressure.pk].status == VariableStatus.INACTIVE
    assert [p.value for p in by_meter[pressure.pk].points] == [Decimal("6")]


def test_asset_condition_back_to_normal_and_table_limit(in_a, scenario):
    bearing, _vibration, _pressure = scenario
    for i in range(TABLE_LIMIT + 5):
        record_meter_reading(meter=bearing, value="50", read_at=_at(1) + datetime.timedelta(minutes=i))
    row = next(
        r for r in asset_condition(membership=in_a.admin, asset=in_a.asset, now=NOW) if r.meter == bearing
    )
    assert row.status == VariableStatus.NORMAL
    assert row.active_alerts == 0  # normalizada
    assert len(row.recent) == TABLE_LIMIT
    assert row.recent[0].at > row.recent[-1].at


def test_variable_with_rules_and_no_readings_is_not_reported_as_normal(in_a):
    meter = variable(in_a.asset)
    condition.create_rule(membership=in_a.admin, meter=meter, operator="gt", threshold="80")
    (row,) = asset_condition(membership=in_a.admin, asset=in_a.asset, now=NOW)
    assert row.status == VariableStatus.NO_READINGS
    assert row.points == []


def test_asset_condition_is_empty_without_variables(in_a):
    Meter.objects.create(asset=in_a.asset, kind=MeterKind.HOURS)
    assert asset_condition(membership=in_a.admin, asset=in_a.asset) == []


# --- SVG de la tendencia (función pura) -----------------------------------------------


def test_trend_scale_pads_the_range_and_opens_a_single_value():
    assert trend_scale([Decimal("0"), Decimal("100")]) == (Decimal("-10"), Decimal("110"))
    assert trend_scale([Decimal("5")]) == (Decimal("4"), Decimal("6"))
    assert trend_scale([]) is None


def test_build_trend_draws_line_dots_and_threshold_with_known_coordinates():
    start = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
    end = start + datetime.timedelta(days=10)
    points = [
        TrendPoint(at=start, value=Decimal("0")),
        TrendPoint(at=end, value=Decimal("100"), out_of_range=True),
    ]
    chart = build_trend(points, [Decimal("50")], start=start, end=end, title="T <b>", element_id="trend-x")
    svg = str(chart.svg)
    # Escala −10..110 en 180 px útiles (alto 200, margen 10): 0 → y=175; 100 → y=25; 50 → y=100.
    assert '<polyline class="trend__line" points="10.0,175.0 590.0,25.0">' in svg
    assert '<line class="trend__threshold" x1="0" x2="600" y1="100.0" y2="100.0">' in svg
    assert '<path class="trend__dot" d="M10.0 175.0h0">' in svg
    assert '<path class="trend__dot trend__dot--alarm" d="M590.0 25.0h0">' in svg
    assert '<title id="trend-x-title">T &lt;b&gt;</title>' in svg  # título escapado
    assert 'role="img"' in svg and "style" not in svg and "#" not in svg  # sin estilos ni colores
    assert (chart.low, chart.high) == (Decimal("-10"), Decimal("110"))


def test_build_trend_without_points_and_with_many_points():
    start = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
    end = start + datetime.timedelta(days=90)
    empty = build_trend([], [Decimal("5")], start=start, end=end, title="x", element_id="e")
    assert "polyline" not in str(empty.svg) and empty.low is None
    many = [
        TrendPoint(at=start + datetime.timedelta(hours=i), value=Decimal(i % 7), out_of_range=i == 3)
        for i in range(200)
    ]
    svg = str(build_trend(many, [], start=start, end=end, title="x", element_id="m").svg)
    assert svg.count('class="trend__dot') == 1  # con muchas lecturas solo se marcan las fuera de rango
    assert svg.count("trend__dot--alarm") == 1


# --- Pantallas -------------------------------------------------------------------------


def _recent(meter, value, days_ago):
    record_meter_reading(meter=meter, value=value, read_at=timezone.now() - datetime.timedelta(days=days_ago))


def test_asset_detail_condition_section_shows_status_trend_table_and_alerts_link(client, tenant_a):
    with tenant_context(tenant_a.org):
        meter = variable(tenant_a.asset, name="Rodamiento")
        condition.create_rule(membership=tenant_a.admin, meter=meter, operator="gt", threshold="80")
        _recent(meter, "-5", 3)
        _recent(meter, "85", 1)
        hours = Meter.objects.create(asset=tenant_a.asset, kind=MeterKind.HOURS)
    login(client, tenant_a.admin)
    body = client.get(reverse("asset_detail", args=[tenant_a.asset.pk])).content.decode()
    assert 'id="condition"' in body
    assert "Alarma" in body
    assert '<svg class="chart-trend"' in body and "trend__threshold" in body
    assert "Fuera de rango" in body and "-5" in body
    assert f"{reverse('condition_alert_list')}?asset={tenant_a.asset.pk}&amp;status=all" in body
    assert "1 alerta activa" in body
    assert reverse("asset_variable_create", args=[tenant_a.asset.pk]) in body
    assert reverse("asset_variable_edit", args=[tenant_a.asset.pk, meter.pk]) in body
    assert f"{reverse('condition_rule_create')}?meter={meter.pk}" in body
    # Los medidores de uso siguen en "Medidores"; la variable no se repite allí.
    assert f'id="meter-{hours.pk}"' in body
    assert body.count(f'id="meter-{meter.pk}"') == 1
    # La lectura de la variable admite negativos: el campo no tiene min="0".
    reading_url = reverse("meter_reading", args=[tenant_a.asset.pk, meter.pk])
    form = body.split(f'action="{reading_url}"')[1].split("</form>")[0]
    assert 'min="0"' not in form and "is_reset" not in form


def test_asset_detail_lists_deactivated_variables_for_reactivation(client, tenant_a):
    with tenant_context(tenant_a.org):
        meter = variable(tenant_a.asset, kind=MeterKind.CURRENT, name="Fase R")
        set_variable_active(membership=tenant_a.admin, meter=meter, is_active=False)
    login(client, tenant_a.admin)
    body = client.get(reverse("asset_detail", args=[tenant_a.asset.pk])).content.decode()
    assert "1 variable desactivada" in body and "Fase R" in body and "Reactivar" in body
    login(client, tenant_a.tech)
    body = client.get(reverse("asset_detail", args=[tenant_a.asset.pk])).content.decode()
    assert "Fase R" in body and "Reactivar" not in body


def test_variable_create_edit_and_deactivate_screens(client, tenant_a):
    login(client, tenant_a.admin)
    url = reverse("asset_variable_create", args=[tenant_a.asset.pk])
    assert "Agregar variable de condición" in client.get(url).content.decode()
    response = client.post(url, {"kind": "temperature", "name": "Rodamiento", "unit": ""})
    assert response.status_code == 302
    with tenant_context(tenant_a.org):
        meter = Meter.objects.get(kind="temperature")
        assert (meter.name, meter.unit) == ("Rodamiento", "°C")
    # Clave repetida: error de formulario amable, sin 500.
    again = client.post(url, {"kind": "temperature", "name": "rodamiento"})
    assert again.status_code == 200 and "ya tiene un medidor" in again.content.decode()
    # Un tipo de uso no se ofrece.
    assert client.post(url, {"kind": "hours"}).status_code == 200
    edit = reverse("asset_variable_edit", args=[tenant_a.asset.pk, meter.pk])
    assert 'name="kind"' not in client.get(edit).content.decode()
    assert client.post(edit, {"name": "Rodamiento LA", "unit": "°C"}).status_code == 302
    active = reverse("asset_variable_active", args=[tenant_a.asset.pk, meter.pk])
    assert client.post(active, {"is_active": "0"}).status_code == 302
    with tenant_context(tenant_a.org):
        meter.refresh_from_db()
        assert (meter.name, meter.is_active) == ("Rodamiento LA", False)
    client.post(active, {"is_active": "1"})
    with tenant_context(tenant_a.org):
        meter.refresh_from_db()
        assert meter.is_active


def test_variable_screens_reject_usage_meters(client, tenant_a):
    with tenant_context(tenant_a.org):
        hours = Meter.objects.create(asset=tenant_a.asset, kind=MeterKind.HOURS)
    login(client, tenant_a.admin)
    assert client.get(reverse("asset_variable_edit", args=[tenant_a.asset.pk, hours.pk])).status_code == 404
    response = client.post(
        reverse("asset_variable_active", args=[tenant_a.asset.pk, hours.pk]), {"is_active": "0"}
    )
    assert response.status_code == 404


def test_design_system_shows_the_trend_and_variable_states(client, tenant_a):
    tenant_a.admin.user.is_staff = True
    tenant_a.admin.user.save(update_fields=["is_staff"])
    login(client, tenant_a.admin)
    body = client.get(reverse("design_system")).content.decode()
    assert '<svg class="chart-trend"' in body
    for label in ("Normal", "Alarma", "Sin reglas", "Sin lecturas", "Desactivada"):
        assert label in body
