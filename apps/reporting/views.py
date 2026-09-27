from django.contrib import messages
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _

from apps.accounts.models import Membership
from apps.accounts.permissions import Perm
from apps.assets.attributes import attribute_rows
from apps.assets.selectors import active_attributes, asset_failure_history, asset_list, visible_assets
from apps.assets.views import _list_filters as asset_list_filters
from apps.attachments.models import Purpose, TargetType
from apps.attachments.selectors import attachments_for
from apps.core.decorators import membership_required
from apps.maintenance.selectors import active_assignments_for_asset
from apps.onboarding.selectors import onboarding_for_dashboard
from apps.work.models import WorkOrder
from apps.work.selectors import (
    filter_backlog,
    filter_work_orders,
    visible_work_orders,
    work_order_detail,
    work_order_list,
)

from .excel import MAX_EXPORT_ROWS, XLSX_CONTENT_TYPE, assets_xlsx, backlog_xlsx, work_orders_xlsx
from .forms import DashboardFilterForm
from .pdf import asset_sheet_pdf, work_order_pdf
from .selectors import DOWN_STATUSES, dashboard_kpis


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
    }
    return render(request, "reporting/dashboard.html", context)


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
