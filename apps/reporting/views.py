import datetime

from django.contrib import messages
from django.core.paginator import Paginator
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _

from apps.accounts.models import Membership
from apps.accounts.permissions import Perm, has_perm
from apps.assets.attributes import attribute_rows
from apps.assets.selectors import active_attributes, asset_failure_history, asset_list, visible_assets
from apps.assets.views import _list_filters as asset_list_filters
from apps.attachments.models import Purpose, TargetType
from apps.attachments.selectors import attachments_for
from apps.condition.selectors import active_alert_count
from apps.core.decorators import membership_required
from apps.maintenance.selectors import active_assignments_for_asset
from apps.onboarding.selectors import onboarding_for_dashboard
from apps.organizations.modules import has_module, plan_message
from apps.work.models import WorkOrder
from apps.work.selectors import (
    filter_backlog,
    filter_work_orders,
    visible_work_orders,
    work_order_detail,
    work_order_list,
)

from .excel import (
    MAX_EXPORT_ROWS,
    XLSX_CONTENT_TYPE,
    assets_xlsx,
    backlog_xlsx,
    extended_kpis_xlsx,
    work_orders_xlsx,
)
from .extended import (
    PARETO_CODES,
    PARETO_METRICS,
    TOP_COST_ASSETS,
    backlog_trend,
    cost_report,
    failure_pareto,
    with_bars,
    work_mix,
)
from .forms import DashboardFilterForm, ExtendedKpiFilterForm
from .pdf import asset_sheet_pdf, work_order_pdf
from .selectors import DOWN_STATUSES, KpiFilters, dashboard_kpis


@membership_required
def dashboard(request):
    membership = request.membership
    onboarding = onboarding_for_dashboard(membership=membership)
    if onboarding is not None and onboarding.started_at is None:
        # Organización recién registrada (ORG-03): el primer inicio lleva al asistente.
        return redirect("onboarding")
    is_technician = membership.role == Membership.Role.TECHNICIAN
    filter_form = DashboardFilterForm(request.GET or None, membership=membership)
    filters = None
    if filter_form.is_bound:
        filter_form.is_valid()  # los campos con error se ignoran y se muestran en el formulario
        filters = filter_form.filters()
    context = {
        "kpis": dashboard_kpis(membership=membership, filters=filters),
        "filter_form": filter_form,
        "active_filters": filter_form.active_count if filter_form.is_bound else 0,
        "filters_open": filter_form.is_bound and bool(filter_form.active_count or filter_form.errors),
        "priority_work_orders": work_order_list(membership=membership, scope="open")[:8],
        "my_work_orders": work_order_list(membership=membership, scope="open", assigned_to=request.user)[:8],
        "down_assets": visible_assets(membership=membership)
        .filter(status__in=DOWN_STATUSES)
        .select_related("location"),
        "is_technician": is_technician,
        "onboarding": onboarding,
        # Alertas de condición activas (CBM-05 CA5): solo con el módulo y permiso de verlas.
        "condition_alerts": active_alert_count(membership=membership)
        if has_module(request.organization, "condition") and has_perm(membership, Perm.CONDITION_VIEW)
        else None,
    }
    return render(request, "reporting/dashboard.html", context)


# --- Indicadores ampliados (KPI-02) ----------------------------------------------

COST_PAGE_SIZE = 25


def _extended_inputs(request):
    """Formulario, filtros y opciones del Pareto desde la URL (pantalla y Excel)."""
    form = ExtendedKpiFilterForm(request.GET or None, membership=request.membership)
    filters = KpiFilters(days=84)
    if form.is_bound:
        form.is_valid()  # los campos con error se ignoran y se muestran en el formulario
        filters = form.filters()
    code_kind = request.GET.get("pareto")
    metric = request.GET.get("pareto_by")
    return (
        form,
        filters,
        code_kind if code_kind in PARETO_CODES else "mode",
        metric if metric in PARETO_METRICS else "count",
    )


def _with_query(request, **changes) -> str:
    """La URL actual con algunos parámetros cambiados (None los quita); vuelve a la página 1."""
    query = request.GET.copy()
    query.pop("page", None)
    for key, value in changes.items():
        if value is None:
            query.pop(key, None)
        else:
            query[key] = value
    return f"?{query.urlencode()}" if query else "?"


def _kpis_module_denied(request):
    organization = request.organization
    if has_module(organization, "kpis"):
        return None
    return render(
        request,
        "organizations/plan_module.html",
        {"message": plan_message(organization, "kpis")},
        status=403,
    )


@membership_required
def kpi_report_view(request):
    """Indicadores ampliados (KPI-02): mismos roles, filtros y alcance que el tablero."""
    if denied := _kpis_module_denied(request):
        return denied
    membership = request.membership
    form, filters, code_kind, metric = _extended_inputs(request)
    now = timezone.now()
    window_start, window_end = filters.window(now)
    costs = cost_report(membership=membership, filters=filters, now=now)
    all_costs = request.GET.get("costs") == "all"
    peak = costs.by_asset.first()
    peak = peak["total"] if peak else 0
    if all_costs:
        cost_page = Paginator(costs.by_asset, COST_PAGE_SIZE).get_page(request.GET.get("page"))
        cost_rows = with_bars(cost_page.object_list, peak)
    else:
        cost_page = None
        cost_rows = with_bars(costs.by_asset[: TOP_COST_ASSETS + 1], peak)
    more_assets = not all_costs and len(cost_rows) > TOP_COST_ASSETS
    context = {
        "filter_form": form,
        "active_filters": form.active_count if form.is_bound else 0,
        "filters_open": form.is_bound and bool(form.active_count or form.errors),
        "period_start": timezone.localdate(window_start),
        "period_end": timezone.localdate(window_end - datetime.timedelta(microseconds=1)),
        "backlog": backlog_trend(membership=membership, filters=filters, now=now),
        "mix": work_mix(membership=membership, filters=filters, now=now),
        "costs": costs,
        "cost_rows": cost_rows[:TOP_COST_ASSETS] if not all_costs else cost_rows,
        "cost_page": cost_page,
        "page": cost_page,
        "all_costs": all_costs,
        "more_assets": more_assets,
        "pareto": failure_pareto(
            membership=membership, filters=filters, code_kind=code_kind, metric=metric, now=now
        ),
        "pareto_code_links": [
            (label, _with_query(request, pareto=value), value == code_kind)
            for value, label in (("mode", _("Por modo de falla")), ("cause", _("Por causa")))
        ],
        "pareto_metric_links": [
            (label, _with_query(request, pareto_by=value), value == metric)
            for value, label in (("count", _("Cantidad")), ("downtime", _("Detención")))
        ],
        "pareto_code": code_kind,
        "pareto_metric": metric,
        "all_costs_href": _with_query(request, costs="all"),
        "top_costs_href": _with_query(request, costs=None),
        "export_query": request.GET.urlencode(),
    }
    return render(request, "reporting/kpi_report.html", context)


@membership_required
def kpi_report_xlsx_view(request):
    """Indicadores ampliados a Excel (KPI-03): una hoja por indicador, con los filtros aplicados."""
    if denied := _kpis_module_denied(request):
        return denied
    membership = request.membership
    _form, filters, code_kind, metric = _extended_inputs(request)
    now = timezone.now()
    content = extended_kpis_xlsx(
        filter_rows=_filter_rows(filters, now),
        backlog=backlog_trend(membership=membership, filters=filters, now=now),
        mix=work_mix(membership=membership, filters=filters, now=now),
        costs=cost_report(membership=membership, filters=filters, now=now),
        pareto=failure_pareto(
            membership=membership, filters=filters, code_kind=code_kind, metric=metric, now=now
        ),
    )
    response = HttpResponse(content, content_type=XLSX_CONTENT_TYPE)
    filename = f"indicadores-{request.organization.slug}-{timezone.localdate():%Y%m%d}.xlsx"
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


def _filter_rows(filters, now):
    """Filtros aplicados, legibles, para la primera hoja del Excel."""
    window_start, window_end = filters.window(now)
    rows = [
        (
            _("Período"),
            f"{timezone.localdate(window_start):%d-%m-%Y} – "
            f"{timezone.localdate(window_end - datetime.timedelta(microseconds=1)):%d-%m-%Y}",
        )
    ]
    if filters.location is not None:
        rows.append((_("Sitio o ubicación"), f"{filters.location.name} ({filters.location.code})"))
    if filters.asset_class is not None:
        rows.append((_("Clase de activo"), filters.asset_class.name))
    if filters.criticality:
        rows.append((_("Criticidad"), filters.criticality))
    if filters.asset is not None:
        rows.append((_("Activo"), f"{filters.asset.code} · {filters.asset.name}"))
    return rows


# --- Exportación a Excel (KPI-03) ------------------------------------------------


def _back_to(name, request):
    query = request.GET.urlencode()
    return f"{reverse(name)}?{query}" if query else reverse(name)


def _xlsx_response(request, *, queryset, build, list_name, prefix):
    """Excel de la lista con sus filtros actuales, o de vuelta a la lista si supera el tope."""
    total = queryset.count()
    if total > MAX_EXPORT_ROWS:
        messages.error(
            request,
            _("Son %(total)s filas y un Excel admite hasta %(max)s. Filtra la lista y exporta por partes.")
            % {"total": total, "max": MAX_EXPORT_ROWS},
        )
        return redirect(_back_to(list_name, request))
    response = HttpResponse(build(queryset), content_type=XLSX_CONTENT_TYPE)
    filename = f"{prefix}-{request.organization.slug}-{timezone.localdate():%Y%m%d}.xlsx"
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


@membership_required
def asset_list_xlsx_view(request):
    """Lista de activos a Excel: mismos filtros (búsqueda, estado, padre, incompletos) y alcance."""
    search, status, parent, incomplete = asset_list_filters(request)
    assets = asset_list(
        membership=request.membership, search=search, status=status, parent=parent, incomplete=incomplete
    )
    return _xlsx_response(
        request, queryset=assets, build=assets_xlsx, list_name="asset_list", prefix="activos"
    )


@membership_required
def work_order_list_xlsx_view(request):
    """Lista de OT a Excel: misma vista (scope), tipo, filtros y alcance que la pantalla."""
    return _xlsx_response(
        request,
        queryset=filter_work_orders(params=request.GET, membership=request.membership).queryset,
        build=work_orders_xlsx,
        list_name="work_order_list",
        prefix="ordenes-de-trabajo",
    )


@membership_required(perm=Perm.WORK_ORDER_MANAGE)
def backlog_xlsx_view(request):
    """Backlog priorizado a Excel (REQ-04): mismos filtros, alcance y orden que la pantalla."""
    queryset, _form = filter_backlog(params=request.GET, membership=request.membership)
    return _xlsx_response(
        request, queryset=queryset, build=backlog_xlsx, list_name="work_order_backlog", prefix="backlog"
    )


# --- PDF de ficha de activo y OT (KPI-03) ----------------------------------------

# Registros recientes en la ficha impresa; el historial completo está en la app.
PDF_HISTORY_LIMIT = 15


def _pdf_response(content, filename):
    response = HttpResponse(content, content_type="application/pdf")
    response["Content-Disposition"] = f'inline; filename="{filename}"'
    return response


@membership_required
def asset_pdf_view(request, pk):
    """Ficha del activo en PDF: mismo alcance que la ficha en pantalla."""
    membership = request.membership
    asset = get_object_or_404(
        visible_assets(membership=membership).select_related("asset_class", "location", "parent"), pk=pk
    )
    work_orders = (
        visible_work_orders(membership=membership)
        .filter(asset=asset)
        .order_by("-created_at")[:PDF_HISTORY_LIMIT]
    )
    pdf = asset_sheet_pdf(
        organization=request.organization,
        asset=asset,
        attributes=attribute_rows(asset, active_attributes(asset.asset_class)),
        meters=asset.meters.filter(is_active=True).order_by("name"),
        assignments=active_assignments_for_asset(asset=asset),
        work_orders=work_orders,
        failures=asset_failure_history(asset)[:PDF_HISTORY_LIMIT],
    )
    return _pdf_response(pdf, f"activo-{asset.code}.pdf")


@membership_required
def work_order_pdf_view(request, pk):
    """OT en PDF: mismo alcance que el detalle de la OT."""
    try:
        work_order = work_order_detail(pk, membership=request.membership)
    except WorkOrder.DoesNotExist as exc:
        raise Http404 from exc
    signatures = [
        a
        for a in attachments_for(target_type=TargetType.WORK_ORDER, target_id=work_order.pk)
        if a.purpose == Purpose.SIGNATURE
    ]
    pdf = work_order_pdf(
        organization=request.organization,
        work_order=work_order,
        helpers=list(work_order.helpers.all()),
        checklist=list(work_order.checklist.all()),
        signature=signatures[-1] if signatures else None,
    )
    return _pdf_response(pdf, f"ot-{work_order.code}.pdf")
