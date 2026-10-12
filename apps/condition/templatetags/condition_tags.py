"""Etiquetas de la sección "Condición" de la ficha del activo (CBM-07)."""

from django import template
from django.utils.html import format_html
from django.utils.translation import gettext as _

from apps.ui.templatetags.ui import icon, num

from ..charts import build_trend
from ..selectors import VariableStatus

register = template.Library()

# Estado de una variable: tono del badge, ícono y texto (siempre ícono + texto).
_STATUS_BADGES = {
    VariableStatus.ALARM: ("error", "alert"),
    VariableStatus.NORMAL: ("success", "check"),
    VariableStatus.NO_RULES: ("neutral", "dot"),
    VariableStatus.NO_READINGS: ("neutral", "clock"),
    VariableStatus.INACTIVE: ("neutral", "x"),
}


def status_label(status: str) -> str:
    return {
        VariableStatus.ALARM: _("Alarma"),
        VariableStatus.NORMAL: _("Normal"),
        VariableStatus.NO_RULES: _("Sin reglas"),
        VariableStatus.NO_READINGS: _("Sin lecturas"),
        VariableStatus.INACTIVE: _("Desactivada"),
    }.get(status, "")


@register.simple_tag
def variable_status_badge(row):
    """Estado de una variable (normal, alarma, sin reglas, sin lecturas, desactivada) con ícono + texto."""
    tone, name = _STATUS_BADGES.get(row.status, ("neutral", "dot"))
    return format_html(
        '<span class="badge badge--{}">{}{}</span>', tone, icon(name), status_label(row.status)
    )


@register.simple_tag
def trend_chart(row):
    """Tendencia de 90 días de una variable (`{% trend_chart row as chart %}`): `chart.svg`,
    `chart.low` y `chart.high` (escala, para el texto junto al gráfico)."""
    meter = row.meter
    variable = meter.name or meter.get_kind_display()
    if row.points:
        values = [p.value for p in row.points]
        title = _("%(variable)s, últimos 90 días: %(n)s lecturas, entre %(low)s y %(high)s %(unit)s.") % {
            "variable": variable,
            "n": len(values),
            "low": num(min(values)),
            "high": num(max(values)),
            "unit": meter.unit,
        }
    else:
        title = _("%(variable)s: sin lecturas en los últimos 90 días.") % {"variable": variable}
    return build_trend(
        row.points,
        row.thresholds,
        start=row.since,
        end=row.until,
        title=title,
        element_id=f"trend-{meter.pk}",
    )
