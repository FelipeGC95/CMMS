from decimal import Decimal

from django import template
from django.utils.html import format_html
from django.utils.safestring import mark_safe

from apps.core.tenancy import get_current_organization

register = template.Library()

# Íconos de trazo (24×24), heredan el color del texto.
_ICONS = {
    "check": '<path d="M5 12.5l4.5 4.5L19 7.5"/>',
    "x": '<path d="M6 6l12 12M18 6L6 18"/>',
    "alert": '<path d="M12 3.5L2.5 20h19L12 3.5z"/><path d="M12 10v4.5M12 17.5v.01"/>',
    "stop": '<circle cx="12" cy="12" r="9"/><path d="M9 9l6 6M15 9l-6 6"/>',
    "info": '<circle cx="12" cy="12" r="9"/><path d="M12 11v5.5M12 7.5v.01"/>',
    "dot": '<circle cx="12" cy="12" r="4" fill="currentColor"/>',
    "wrench": '<path d="M14.5 6.5a4 4 0 0 0 5 5l-8 8a2.1 2.1 0 0 1-3-3l8-8a4 4 0 0 0-2-2z"/><path d="M14.5 6.5L17 4l3 3-2.5 2.5"/>',
    "clock": '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
    "calendar": '<rect x="3.5" y="5" width="17" height="15" rx="2"/><path d="M3.5 10h17M8 3v4M16 3v4"/>',
    "home": '<path d="M4 11l8-7 8 7v9h-5v-6H9v6H4z"/>',
    "box": '<path d="M12 3l8.5 4.5v9L12 21l-8.5-4.5v-9L12 3z"/><path d="M3.5 7.5L12 12l8.5-4.5M12 12v9"/>',
    "menu": '<path d="M4 7h16M4 12h16M4 17h16"/>',
    "list": '<path d="M9 6h11M9 12h11M9 18h11M4.5 6h.01M4.5 12h.01M4.5 18h.01"/>',
    "megaphone": '<path d="M4 10v4h3l7 4V6L7 10H4z"/><path d="M17.5 9a4 4 0 0 1 0 6"/>',
    "print": '<path d="M7 9V4h10v5"/><rect x="4" y="9" width="16" height="7" rx="2"/><path d="M7 14h10v6H7z"/>',
    "play": '<path d="M8 5.5v13l10-6.5-10-6.5z"/>',
    "contrast": '<circle cx="12" cy="12" r="9"/><path d="M12 3a9 9 0 0 1 0 18z" fill="currentColor"/>',
    "camera": '<path d="M4 8h3l2-2.5h6L17 8h3v11H4z"/><circle cx="12" cy="13" r="3.5"/>',
}


@register.simple_tag
def icon(name, label=""):
    aria = format_html(' role="img" aria-label="{}"', label) if label else ' aria-hidden="true"'
    return mark_safe(
        f'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" '
        f'stroke-linecap="round" stroke-linejoin="round"{aria}>{_ICONS[name]}</svg>'
    )


_TONE_ICON = {
    "success": "check",
    "warning": "wrench",
    "error": "stop",
    "info": "info",
    "neutral": "dot",
}

_STATUS_TONES = {
    # Activos
    "operational": "success",
    "stopped": "error",
    "in_maintenance": "warning",
    "retired": "neutral",
    # OT
    "requested": "warning",
    "planned": "neutral",
    "scheduled": "info",
    "in_progress": "info",
    "on_hold": "warning",
    "completed": "success",
    "closed": "success",
    "cancelled": "neutral",
    # Solicitudes (REQ-01 CA3)
    "pending": "warning",
    "accepted": "success",
    "rejected": "error",
    "merged": "neutral",
}


def work_order_status_label(value):
    """Nombre visible de un estado de OT: el propio de la organización activa o el traducido (WO-02 CA5).

    Lee la organización del contexto de tenant, ya cargada por el middleware: sin consultas extra.
    """
    from apps.work.models import WorkOrder

    organization = get_current_organization()
    custom = (organization.work_order_status_labels or {}).get(value) if organization else None
    if custom:
        return custom
    try:
        return WorkOrder.Status(value).label
    except ValueError:
        return value


@register.simple_tag
def status_badge(obj):
    """Badge de estado con ícono + texto (nunca solo color)."""
    from apps.work.models import WorkOrder

    tone = _STATUS_TONES.get(obj.status, "neutral")
    label = work_order_status_label(obj.status) if isinstance(obj, WorkOrder) else obj.get_status_display()
    return format_html(
        '<span class="badge badge--{}">{}{}</span>',
        tone,
        icon(_TONE_ICON[tone]),
        label,
    )


_PRIORITY_TONES = {1: "error", 2: "warning", 3: "neutral", 4: "neutral"}


@register.simple_tag
def priority_badge(work_order):
    tone = _PRIORITY_TONES[work_order.priority]
    return format_html(
        '<span class="badge badge--{}">{}{}</span>',
        tone,
        icon("alert" if work_order.priority <= 2 else "dot"),
        work_order.get_priority_display(),
    )


@register.filter
def hours(value, digits=1):
    if value is None:
        return "—"
    return f"{value:.{digits}f}".replace(".", ",")


@register.filter
def pct(value):
    if value is None:
        return "—"
    return f"{value:.1f}".replace(".", ",")


@register.simple_tag(takes_context=True)
def nav_current(context, *prefixes):
    path = context["request"].path
    if any(path == p if p == "/" else path.startswith(p) for p in prefixes):
        return mark_safe('aria-current="page"')
    return ""


@register.filter
def status_label(value):
    return work_order_status_label(value)


@register.filter
def num(value):
    """Número con separador de miles y coma decimal (es-CL), sin ceros sobrantes."""
    if value is None or value == "":
        return "—"
    text = f"{Decimal(value):,.3f}".rstrip("0").rstrip(".")
    return text.replace(",", " ").replace(".", ",").replace(" ", ".")
