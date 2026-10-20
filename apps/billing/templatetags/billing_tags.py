"""Formato de montos de la facturación (es-CL): pesos con punto de miles y UF con coma decimal."""

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from django import template

register = template.Library()


@register.filter
def clp(value) -> str:
    """59815 → "$59.815"; vacío si no hay monto."""
    if value in (None, ""):
        return ""
    try:
        amount = int(Decimal(str(value)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    except (InvalidOperation, ValueError):
        return ""
    return "$" + f"{amount:,}".replace(",", ".")


@register.filter
def uf(value) -> str:
    """Decimal("13.5000") → "13,5"; hasta dos decimales, sin ceros de más."""
    if value in (None, ""):
        return ""
    try:
        amount = Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        return ""
    text = f"{amount:,.2f}".rstrip("0").rstrip(".")
    return text.replace(",", "\u0000").replace(".", ",").replace("\u0000", ".")
