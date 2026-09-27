from django.shortcuts import render

from apps.accounts.models import Membership
from apps.assets.selectors import visible_assets
from apps.core.decorators import membership_required
from apps.work.selectors import work_order_list

from .forms import DashboardFilterForm
from .selectors import DOWN_STATUSES, dashboard_kpis


@membership_required
def dashboard(request):
    membership = request.membership
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
    }
    return render(request, "reporting/dashboard.html", context)
