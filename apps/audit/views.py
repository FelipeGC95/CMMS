from django.core.paginator import Paginator
from django.shortcuts import render

from apps.accounts.permissions import Perm
from apps.core.decorators import membership_required

from .forms import AuditFilterForm
from .selectors import audit_events, audited_types


@membership_required(perm=Perm.AUDIT_VIEW)
def audit_list_view(request):
    form = AuditFilterForm(request.GET or None, target_types=audited_types())
    filters = form.cleaned_data if form.is_valid() else {}
    events = audit_events(membership=request.membership, **filters)
    page = Paginator(events, 50).get_page(request.GET.get("page"))
    return render(request, "audit/list.html", {"form": form, "page": page})
