"""Tendencia de una variable como SVG generado en el servidor (CBM-07 CA2).

Función pura (sin base de datos ni plantillas): recibe los puntos y los umbrales y devuelve el
SVG con clases (los colores salen de los tokens en static/css/components.css). Sin JS ni estilos
en línea, compatible con la CSP estricta. El texto (fechas, escala y leyenda) va en HTML junto
al gráfico, legible a cualquier ancho; el SVG se estira al ancho de la tarjeta
(`preserveAspectRatio="none"`, trazos que no se deforman con `vector-effect`).
"""

import datetime
from dataclasses import dataclass
from decimal import Decimal

from django.utils.html import escape
from django.utils.safestring import SafeString, mark_safe

WIDTH, HEIGHT = 600, 200
PAD = 10  # margen interior para que los puntos de los bordes no se corten
# Con más puntos que esto solo se marcan los fuera de rango (la línea basta para leer la forma).
MAX_DOTS = 60


@dataclass(frozen=True)
class TrendPoint:
    at: datetime.datetime
    value: Decimal
    out_of_range: bool = False


@dataclass(frozen=True)
class TrendChart:
    svg: SafeString
    low: Decimal | None  # escala vertical (incluye los umbrales)
    high: Decimal | None


def trend_scale(values: list[Decimal]) -> tuple[Decimal, Decimal] | None:
    """Mínimo y máximo de la escala, con un 10 % de aire; un valor único se abre en ±1."""
    if not values:
        return None
    low, high = min(values), max(values)
    if low == high:
        return low - 1, high + 1
    margin = (high - low) / 10
    return low - margin, high + margin


def _fmt(number: float) -> str:
    return f"{number:.1f}"  # siempre con punto: coordenadas SVG, nunca localizadas


def build_trend(
    points: list[TrendPoint],
    thresholds: list[Decimal],
    *,
    start: datetime.datetime,
    end: datetime.datetime,
    title: str,
    element_id: str,
) -> TrendChart:
    """SVG de la tendencia entre `start` y `end` con una línea por umbral.

    `points` en orden de fecha. Sin puntos devuelve un SVG vacío (solo el fondo).
    """
    scale = trend_scale([p.value for p in points] + list(thresholds)) if points else None
    title_id = f"{element_id}-title"
    parts = [
        f'<svg class="chart-trend" id="{escape(element_id)}" viewBox="0 0 {WIDTH} {HEIGHT}" '
        f'preserveAspectRatio="none" role="img" aria-labelledby="{escape(title_id)}" focusable="false">',
        f'<title id="{escape(title_id)}">{escape(title)}</title>',
    ]
    if scale is None:
        parts.append("</svg>")
        return TrendChart(svg=mark_safe("".join(parts)), low=None, high=None)  # título y id escapados arriba

    low, high = scale
    span_seconds = max((end - start).total_seconds(), 1.0)
    span_value = float(high - low)

    def x_of(at: datetime.datetime) -> float:
        ratio = min(max((at - start).total_seconds() / span_seconds, 0.0), 1.0)
        return PAD + ratio * (WIDTH - 2 * PAD)

    def y_of(value: Decimal) -> float:
        ratio = float(value - low) / span_value
        return HEIGHT - PAD - ratio * (HEIGHT - 2 * PAD)

    for threshold in sorted(set(thresholds)):
        y = _fmt(y_of(threshold))
        parts.append(f'<line class="trend__threshold" x1="0" x2="{WIDTH}" y1="{y}" y2="{y}"></line>')
    coordinates = [(_fmt(x_of(p.at)), _fmt(y_of(p.value))) for p in points]
    if len(points) > 1:
        parts.append(
            '<polyline class="trend__line" points="'
            + " ".join(f"{x},{y}" for x, y in coordinates)
            + '"></polyline>'
        )
    for point, (x, y) in zip(points, coordinates, strict=True):
        if point.out_of_range:
            parts.append(f'<path class="trend__dot trend__dot--alarm" d="M{x} {y}h0"></path>')
        elif len(points) <= MAX_DOTS:
            parts.append(f'<path class="trend__dot" d="M{x} {y}h0"></path>')
    parts.append("</svg>")
    return TrendChart(svg=mark_safe("".join(parts)), low=low, high=high)  # título y id escapados arriba
