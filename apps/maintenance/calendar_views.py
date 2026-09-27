"""Calendario de preventivos (PM-06 CA1): mes, resumen de 12 meses y Excel.

Lo ven los roles que ejecutan o gestionan OT, dentro de su alcance por sitio.
La proyección no crea OT: la calcula `selectors.plan_calendar` en memoria.
"""

import calendar as pycalendar
import datetime
from decimal import Decimal

from django.http import HttpResponse
from django.shortcuts import render
from django.utils import timezone
from django.utils.dates import WEEKDAYS_ABBR
from django.utils.translation import gettext as _

from apps.core.decorators import membership_required
from apps.reporting.excel import XLSX_CONTENT_TYPE, _workbook

from .forms import CalendarFilterForm
from .models import add_months, format_number
from .projection import MAX_OCCURRENCES
from .selectors import CALENDAR_PERM, calendar_months, plan_calendar

calendar_required = membership_required(perm=CALENDAR_PERM)

# Ocurrencias por día en la grilla del mes; el resto queda en la lista por día.
GRID_ITEMS_PER_DAY = 3


def _filters(request):
    form = CalendarFilterForm(request.GET or None, membership=request.membership)
    data = form.cleaned_data if form.is_valid() else {}
    filters = {
        "location": data.get("location"),
        "asset_class": data.get("asset_class"),
        "asset_q": (data.get("asset") or "").strip(),
        "plan": data.get("plan"),
    }
    return form, filters, sum(1 for value in filters.values() if value)


def _query(request, **changes):
    params = request.GET.copy()
    for key, value in changes.items():
        params[key] = value
    return params.urlencode()


def _month_param(request, first, last):
    """Mes pedido (?month=AAAA-MM) dentro de la ventana de 12 meses; si no, el actual."""
    try:
        month = datetime.datetime.strptime(request.GET.get("month", ""), "%Y-%m").date()
    except ValueError:
        return first
    return min(max(month, first), last)


def _year_window(today):
    first = today.replace(day=1)
    return first, add_months(first, 12) - datetime.timedelta(days=1)


def _hours(minutes):
    return format_number((Decimal(minutes) / 60).quantize(Decimal("0.1")))


@calendar_required
def plan_calendar_view(request):
    today = timezone.localdate()
    first, year_end = _year_window(today)
    last = add_months(first, 11)
    view = "year" if request.GET.get("view") == "year" else "month"
    form, filters, active_filters = _filters(request)
    context = {
        "view": view,
        "filter_form": form,
        "active_filters": active_filters,
        "today": today,
        "max_occurrences": MAX_OCCURRENCES,
        "month_query": _query(request, view="month"),
        "year_query": _query(request, view="year"),
        "export_query": request.GET.urlencode(),
    }
    if view == "year":
        result = plan_calendar(membership=request.membership, start=first, end=year_end, **filters)
        months = calendar_months(result)
        for row in months:
            row["hours"] = _hours(row["minutes"])
            row["query"] = _query(request, view="month", month=f"{row['month']:%Y-%m}")
        context.update(
            result=result,
            months=months,
            total_count=sum(row["count"] for row in months),
            total_hours=_hours(sum(row["minutes"] for row in months)),
        )
    else:
        month = _month_param(request, first, last)
        end = add_months(month, 1) - datetime.timedelta(days=1)
        result = plan_calendar(membership=request.membership, start=month, end=end, **filters)
        by_day = {}
        for occurrence in result.occurrences:
            by_day.setdefault(occurrence.date, []).append(occurrence)
        weeks = [
            [
                {
                    "date": day,
                    "in_month": day.month == month.month,
                    "items": by_day.get(day, [])[:GRID_ITEMS_PER_DAY] if day.month == month.month else [],
                    "more": max(len(by_day.get(day, [])) - GRID_ITEMS_PER_DAY, 0)
                    if day.month == month.month
                    else 0,
                }
                for day in week
            ]
            for week in pycalendar.Calendar(firstweekday=0).monthdatescalendar(month.year, month.month)
        ]
        context.update(
            result=result,
            month=month,
            weeks=weeks,
            weekdays=[WEEKDAYS_ABBR[i] for i in range(7)],
            days=sorted(by_day.items()),
            prev_query=_query(request, view="month", month=f"{add_months(month, -1):%Y-%m}")
            if month > first
            else "",
            next_query=_query(request, view="month", month=f"{add_months(month, 1):%Y-%m}")
            if month < last
            else "",
        )
    return render(request, "maintenance/plan_calendar.html", context)


def _source(occurrence):
    if occurrence.work_order is not None:
        return _("OT %(code)s (%(status)s)") % {
            "code": occurrence.work_order.code,
            "status": occurrence.work_order.get_status_display(),
        }
    return _("Proyectada")


def _note(occurrence):
    notes = []
    if occurrence.absorbed_into is not None:
        notes.append(_("Absorbida en %(plan)s") % {"plan": occurrence.absorbed_into.plan.name})
    if occurrence.overdue:
        notes.append(_("Vencida"))
    if occurrence.no_usage:
        notes.append(_("Sin uso promedio: solo por calendario"))
    return "; ".join(notes)


@calendar_required
def plan_calendar_xlsx_view(request):
    """Proyección de 12 meses con los filtros actuales (un Excel acotado por el tope de ocurrencias)."""
    first, year_end = _year_window(timezone.localdate())
    _form, filters, _active = _filters(request)
    result = plan_calendar(membership=request.membership, start=first, end=year_end, **filters)
    triggers = {"calendar": _("Calendario"), "meter": _("Medidor")}
    headers = [
        _("Fecha"),
        _("Plan"),
        _("Activo"),
        _("Nombre del activo"),
        _("Ubicación"),
        _("Origen"),
        _("Disparador"),
        _("Duración estimada (min)"),
        _("Observación"),
    ]
    rows = (
        (
            o.date,
            o.plan.name if o.plan is not None else (o.work_order.title if o.work_order else ""),
            o.asset.code if o.asset else "",
            o.asset.name if o.asset else "",
            (o.asset.location.name if o.asset and o.asset.location else "")
            or (o.work_order.location.name if o.work_order and o.work_order.location else ""),
            _source(o),
            "" if o.work_order is not None else triggers.get(o.trigger, ""),
            o.estimated_minutes,
            _note(o),
        )
        for o in result.occurrences
    )
    response = HttpResponse(
        _workbook(_("Calendario de preventivos"), headers, rows), content_type=XLSX_CONTENT_TYPE
    )
    filename = f"calendario-preventivos-{request.organization.slug}-{first:%Y%m}.xlsx"
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response
