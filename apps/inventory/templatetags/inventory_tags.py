"""Marcas de stock (INV-12): siempre ícono + texto, nunca solo color."""

from decimal import Decimal

from django import template
from django.urls import NoReverseMatch, reverse
from django.utils.html import format_html
from django.utils.translation import gettext as _

from apps.ui.templatetags.ui import icon

register = template.Library()


def _badge(tone, icon_name, label):
    return format_html('<span class="badge badge--{}">{}{}</span>', tone, icon(icon_name), label)


@register.simple_tag
def stock_mark(on_hand, below_min=False, negative=False, active=True):
    """Estado del stock: Negativo · regularizar, Bajo mínimo, Sin stock o Con stock (e Inactivo)."""
    if not active:
        return _badge("neutral", "dot", _("Inactivo"))
    on_hand = on_hand or Decimal(0)
    if negative or on_hand < 0:
        return _badge("error", "stop", _("Negativo · regularizar"))
    if below_min:
        return _badge("warning", "alert", _("Bajo mínimo"))
    if on_hand == 0:
        return _badge("neutral", "dot", _("Sin stock"))
    return _badge("success", "check", _("Con stock"))


@register.simple_tag
def no_cost_mark(on_hand, avg_cost):
    """Existencia sin costo promedio (INV-08): solo se muestra a quien ve costos."""
    if (on_hand or 0) > 0 and not avg_cost:
        return _badge("info", "info", _("Sin costo"))
    return ""


@register.simple_tag
def reorder_mark(priority):
    """Por qué un repuesto está en la reposición (INV-12 CA4), en el orden en que se atiende."""
    if priority == 0:
        return _badge("error", "stop", _("Negativo · regularizar"))
    if priority == 1:
        return _badge("warning", "alert", _("Reservas sin cubrir"))
    return _badge("warning", "alert", _("Bajo mínimo"))


@register.simple_tag
def optional_url(name, *args):
    """URL si la ruta existe (pantallas de otros paquetes, P4/P5); si no, vacío y la plantilla la oculta."""
    try:
        return reverse(name, args=args)
    except NoReverseMatch:
        return ""
