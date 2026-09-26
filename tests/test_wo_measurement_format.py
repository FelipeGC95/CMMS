"""Mediciones del checklist en formato es-CL: coma decimal, sin miles, ida y vuelta (WO-03 CA2)."""

from decimal import Decimal

import pytest
from django.urls import reverse

from apps.core.tenancy import tenant_context
from apps.maintenance.models import ItemType
from apps.ui.templatetags.ui import measure
from apps.work.services import (
    WorkOrderError,
    add_checklist_item,
    parse_measurement,
    report_failure,
)
from tests.conftest import login


@pytest.fixture(autouse=True)
def _media(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path


def _wo_with_items(tenant, *texts):
    wo = report_failure(
        membership=tenant.viewer, asset=tenant.asset, description="Falla", asset_stopped=False
    ).work_order
    items = [add_checklist_item(work_order=wo, membership=tenant.admin, text=t) for t in texts]
    return wo, items


# --- Mediciones en formato es-CL ---------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("7,5", "7.500"),
        ("7.5", "7.500"),
        (" 1600 ", "1600.000"),
        ("-2,25", "-2.250"),
        (Decimal("1.6"), "1.600"),
    ],
)
def test_parse_measurement_accepts_comma_or_point(raw, expected):
    assert parse_measurement(raw) == Decimal(expected)


@pytest.mark.parametrize("raw", ["", None, "abc", "1.600,5", "NaN", "Infinity", "1e20"])
def test_parse_measurement_rejects_garbage(raw):
    with pytest.raises(WorkOrderError):
        parse_measurement(raw)


@pytest.mark.parametrize(
    "value,text",
    [(Decimal("1.600"), "1,6"), (Decimal("1600.000"), "1600"), (Decimal("6.5"), "6,5"), (None, "—")],
)
def test_measure_filter_uses_decimal_comma_without_thousands(value, text):
    assert measure(value) == text


def test_measurement_round_trips_through_the_mobile_checklist(client, tenant_a):
    with tenant_context(tenant_a.org):
        wo, _items = _wo_with_items(tenant_a)
        item = add_checklist_item(
            work_order=wo,
            membership=tenant_a.admin,
            text="Presión",
            item_type=ItemType.MEASUREMENT,
            unit="bar",
            min_value="1.6",
            max_value="1600",
        )
    login(client, tenant_a.tech)
    url = reverse("checklist_answer", args=[wo.pk, item.pk])

    html = client.post(url, {"value_number": "7,25"}, HTTP_HX_REQUEST="true").content.decode()

    assert 'value="7,25"' in html
    assert "1,6 – 1600" in html
    with tenant_context(tenant_a.org):
        item.refresh_from_db()
        assert (item.value_number, item.result) == (Decimal("7.250"), "ok")
    # Reenviar el valor tal como quedó en el campo no lo cambia.
    client.post(url, {"value_number": "7,25"}, HTTP_HX_REQUEST="true")
    with tenant_context(tenant_a.org):
        item.refresh_from_db()
        assert item.value_number == Decimal("7.250")


def test_invalid_measurement_is_shown_on_the_item(client, tenant_a):
    with tenant_context(tenant_a.org):
        wo, _items = _wo_with_items(tenant_a)
        item = add_checklist_item(
            work_order=wo, membership=tenant_a.admin, text="Presión", item_type=ItemType.MEASUREMENT
        )
    login(client, tenant_a.tech)
    response = client.post(
        reverse("checklist_answer", args=[wo.pk, item.pk]), {"value_number": "siete"}, HTTP_HX_REQUEST="true"
    )
    assert response.status_code == 200
    assert "Ingresa un número" in response.content.decode()
