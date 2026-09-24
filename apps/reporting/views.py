from django.shortcuts import render

from apps.accounts.models import Membership
from apps.assets.models import Asset
from apps.core.decorators import membership_required
from apps.work.selectors import work_order_list

from .selectors import DOWN_STATUSES, dashboard_kpis


@membership_required
def dashboard(request):
    is_technician = request.membership.role == Membership.Role.TECHNICIAN
    context = {
        "kpis": dashboard_kpis(),
        "priority_work_orders": work_order_list(scope="open")[:8],
        "my_work_orders": work_order_list(scope="open", assigned_to=request.user)[:8],
        "down_assets": Asset.objects.filter(status__in=DOWN_STATUSES).select_related("location"),
        "is_technician": is_technician,
    }
    return render(request, "reporting/dashboard.html", context)
