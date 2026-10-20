"""Cálculo del precio (ORG-13 CA2): UF del plan, tramos por activos, anual, cupón y redondeo a CLP.

Funciones puras (sin base de datos): reciben el plan, la organización o sus valores y devuelven un
`Quote`. El monto en pesos siempre se calcula en el servidor, nunca llega del navegador.
"""

import calendar
import datetime
import math
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from .models import PERIOD_MONTHS, Period


class PriceUnavailable(ValueError):
    """El plan no tiene precio (Prueba) ni hay precio acordado (Empresa sin cotizar)."""


def add_months(moment, months: int):
    """Misma hora, `months` meses después; el día se ajusta al último del mes si no existe."""
    month_index = moment.month - 1 + months
    year, month = moment.year + month_index // 12, month_index % 12 + 1
    day = min(moment.day, calendar.monthrange(year, month)[1])
    return moment.replace(year=year, month=month, day=day)


def coupon_percent(organization, *, now: datetime.datetime) -> int:
    """Descuento vigente del cupón aplicado a la organización (0 si no hay o ya venció)."""
    coupon = organization.coupon
    if coupon is None or not coupon.is_active:
        return 0
    if coupon.duration_months and organization.coupon_applied_at:
        if now >= add_months(organization.coupon_applied_at, coupon.duration_months):
            return 0
    return coupon.percent_off


def to_clp(amount_uf: Decimal, uf_value: Decimal) -> int:
    """UF → pesos enteros, redondeo comercial (0,5 hacia arriba)."""
    return int((amount_uf * uf_value).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


@dataclass(frozen=True)
class Quote:
    plan: object
    period: str
    base_monthly_uf: Decimal
    extra_blocks: int
    monthly_uf: Decimal  # base + tramos, antes de descuento
    months_charged: int
    discount_percent: int
    total_uf: Decimal  # lo que se cobra por período
    uf_value: Decimal | None
    amount_clp: int | None  # None si no hay valor de la UF

    @property
    def period_months(self) -> int:
        return PERIOD_MONTHS[self.period]

    @property
    def list_total_uf(self) -> Decimal:
        return self.monthly_uf * self.months_charged


def quote(
    *,
    plan,
    period: str,
    asset_count: int = 0,
    custom_monthly_uf: Decimal | None = None,
    discount_percent: int = 0,
    uf_value: Decimal | None = None,
) -> Quote:
    """Precio de un período del plan. `custom_monthly_uf` (precio acordado) reemplaza al del plan."""
    if period not in PERIOD_MONTHS:
        raise ValueError(f"Período desconocido: {period}")
    base = custom_monthly_uf if custom_monthly_uf is not None else plan.monthly_price_uf
    if base is None:
        raise PriceUnavailable(plan.code)
    base = Decimal(base)
    blocks = 0
    if custom_monthly_uf is None and plan.price_included_assets is not None and plan.price_block_uf:
        over = max(0, asset_count - plan.price_included_assets)
        blocks = math.ceil(over / max(1, plan.price_block_assets))
    monthly = base + blocks * Decimal(plan.price_block_uf if custom_monthly_uf is None else 0)
    months = plan.annual_months_charged if period == Period.ANNUAL else 1
    discount = max(0, min(100, int(discount_percent)))
    total = (monthly * months * (100 - discount) / 100).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
    return Quote(
        plan=plan,
        period=period,
        base_monthly_uf=base,
        extra_blocks=blocks,
        monthly_uf=monthly,
        months_charged=months,
        discount_percent=discount,
        total_uf=total,
        uf_value=uf_value,
        amount_clp=to_clp(total, uf_value) if uf_value else None,
    )


def quote_for_organization(*, organization, plan, period, asset_count, uf_value, now) -> Quote:
    """Precio para la organización: su precio acordado (si el plan es el cotizado) y su cupón."""
    # El precio acordado solo aplica a los planes cotizados (Empresa); Básico y Profesional van a lista.
    custom = None if plan.is_self_service else organization.custom_monthly_uf
    return quote(
        plan=plan,
        period=period,
        asset_count=asset_count,
        custom_monthly_uf=custom,
        discount_percent=coupon_percent(organization, now=now),
        uf_value=uf_value,
    )
