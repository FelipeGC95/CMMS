"""Precio (ORG-13 CA2): UF del plan, tramos de activos, anual, cupón, redondeo; RUT y formato."""

import datetime
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError

from apps.billing.models import Period
from apps.billing.pricing import PriceUnavailable, add_months, coupon_percent, quote, to_clp
from apps.billing.rut import normalize_rut
from apps.billing.templatetags.billing_tags import clp, uf
from apps.organizations.models import Coupon, Organization, Plan

UF = Decimal("39500.00")


def _plan(**kwargs):
    defaults = {"code": "x", "name": "X", "monthly_price_uf": Decimal("4"), "annual_months_charged": 10}
    return Plan(**{**defaults, **kwargs})


def test_basic_monthly_and_annual():
    plan = _plan(monthly_price_uf=Decimal("1.5"))
    monthly = quote(plan=plan, period=Period.MONTHLY, uf_value=UF)
    annual = quote(plan=plan, period=Period.ANNUAL, uf_value=UF)
    assert monthly.total_uf == Decimal("1.5") and monthly.amount_clp == 59250
    # Anual = 10 meses (2 gratis).
    assert annual.total_uf == Decimal("15") and annual.amount_clp == 592500


@pytest.mark.parametrize(
    "assets,blocks,monthly",
    [(0, 0, "4"), (300, 0, "4"), (301, 1, "5"), (400, 1, "5"), (401, 2, "6"), (1000, 7, "11")],
)
def test_pro_adds_one_uf_per_100_assets_over_300(assets, blocks, monthly):
    plan = _plan(price_included_assets=300, price_block_assets=100, price_block_uf=Decimal("1"))
    offer = quote(plan=plan, period=Period.MONTHLY, asset_count=assets, uf_value=UF)
    assert offer.extra_blocks == blocks
    assert offer.total_uf == Decimal(monthly)


def test_coupon_discount_and_rounding_half_up():
    plan = _plan(monthly_price_uf=Decimal("1.5"))
    offer = quote(plan=plan, period=Period.MONTHLY, discount_percent=50, uf_value=Decimal("39501.01"))
    assert offer.total_uf == Decimal("0.75")
    # 0,75 × 39.501,01 = 29.625,7575 → 29.626
    assert offer.amount_clp == 29626
    assert to_clp(Decimal("1"), Decimal("100.50")) == 101
    assert to_clp(Decimal("1"), Decimal("100.49")) == 100


def test_custom_price_replaces_the_plan_and_tiers():
    plan = _plan(monthly_price_uf=Decimal("9"), price_included_assets=10, price_block_uf=Decimal("1"))
    offer = quote(
        plan=plan, period=Period.ANNUAL, asset_count=500, custom_monthly_uf=Decimal("12"), uf_value=UF
    )
    assert offer.extra_blocks == 0 and offer.total_uf == Decimal("120")


def test_without_price_or_uf():
    with pytest.raises(PriceUnavailable):
        quote(plan=_plan(monthly_price_uf=None), period=Period.MONTHLY)
    offer = quote(plan=_plan(), period=Period.MONTHLY, uf_value=None)
    assert offer.amount_clp is None and offer.total_uf == Decimal("4")


def test_coupon_lasts_its_months():
    applied = datetime.datetime(2026, 1, 31, 12, tzinfo=datetime.UTC)
    org = Organization(
        coupon=Coupon(code="lanzamiento", name="Lanzamiento", percent_off=50, duration_months=12),
        coupon_applied_at=applied,
    )
    assert coupon_percent(org, now=applied + datetime.timedelta(days=200)) == 50
    assert coupon_percent(org, now=add_months(applied, 12)) == 0
    org.coupon.is_active = False
    assert coupon_percent(org, now=applied) == 0
    assert coupon_percent(Organization(), now=applied) == 0


def test_add_months_clamps_the_day():
    moment = datetime.datetime(2026, 1, 31, 8, tzinfo=datetime.UTC)
    assert add_months(moment, 1) == datetime.datetime(2026, 2, 28, 8, tzinfo=datetime.UTC)
    assert add_months(moment, 12).year == 2027
    assert add_months(moment, -2) == datetime.datetime(2025, 11, 30, 8, tzinfo=datetime.UTC)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("76123456-0", "76.123.456-0"),
        ("76.123.456-0", "76.123.456-0"),
        ("11.111.111-1", "11.111.111-1"),
        ("12.345.678-5", "12.345.678-5"),
        ("10.000.013-k", "10.000.013-K"),
    ],
)
def test_rut_is_normalized(raw, expected):
    assert normalize_rut(raw) == expected


@pytest.mark.parametrize("raw", ["76.123.456-1", "abc", "", "123456789012-3"])
def test_invalid_rut(raw):
    with pytest.raises(ValidationError):
        normalize_rut(raw)


def test_amount_filters():
    assert clp(59250) == "$59.250" and clp(None) == ""
    assert uf(Decimal("13.5000")) == "13,5" and uf(Decimal("4")) == "4" and uf(Decimal("1234.5")) == "1.234,5"
