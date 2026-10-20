"""Valor de la UF (ORG-13 CA3): fuente pública, respaldo en el último valor y sin valor no se cobra."""

import datetime
from decimal import Decimal

import pytest

from apps.billing import http, uf
from apps.billing.models import UfValue

pytestmark = pytest.mark.django_db

MINDICADOR = {
    "codigo": "uf",
    "serie": [
        {"fecha": "2026-10-19T03:00:00.000Z", "valor": 39876.54},
        {"fecha": "2026-10-18T03:00:00.000Z", "valor": 39870.1},
    ],
}


def _source(monkeypatch, response=None, error=None):
    def fake(method, url, **kwargs):
        if error:
            raise error
        return response

    monkeypatch.setattr(http, "request_json", fake)


def test_refresh_saves_the_day_value(monkeypatch):
    _source(monkeypatch, (200, MINDICADOR))
    row = uf.refresh_uf_value()
    assert row.date == datetime.date(2026, 10, 19) and row.value == Decimal("39876.54")
    _source(monkeypatch, (200, {"serie": [{"fecha": "2026-10-19T03:00:00.000Z", "valor": 39880}]}))
    uf.refresh_uf_value()
    assert UfValue.objects.get(date=datetime.date(2026, 10, 19)).value == Decimal("39880.00")


@pytest.mark.parametrize(
    "response,error",
    [
        (None, http.HttpError("sin red")),
        ((503, {}), None),
        ((200, {"serie": []}), None),
        ((200, {"serie": [{"fecha": "ayer", "valor": "x"}]}), None),
        ((200, {"serie": [{"fecha": "2026-10-19T03:00:00Z", "valor": 3}]}), None),
    ],
)
def test_source_down_keeps_the_last_saved_value(monkeypatch, response, error):
    UfValue.objects.create(date=datetime.date(2026, 10, 10), value=Decimal("39700.00"))
    _source(monkeypatch, response, error)
    assert uf.refresh_uf_value() is None
    fallback = uf.current_uf_value(today=datetime.date(2026, 10, 19))
    assert fallback.value == Decimal("39700.00")
    assert uf.uf_for_charge(today=datetime.date(2026, 10, 19)).value == Decimal("39700.00")


def test_without_any_value_there_is_nothing_to_charge(monkeypatch):
    _source(monkeypatch, error=http.HttpError("sin red"))
    assert uf.current_uf_value() is None
    assert uf.uf_for_charge() is None


def test_future_values_are_not_used():
    UfValue.objects.create(date=datetime.date(2026, 10, 20), value=Decimal("39900.00"))
    UfValue.objects.create(date=datetime.date(2026, 10, 18), value=Decimal("39800.00"))
    assert uf.current_uf_value(today=datetime.date(2026, 10, 19)).value == Decimal("39800.00")
