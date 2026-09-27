from django.contrib import messages
from django.core.paginator import Paginator
from django.http import Http404
from django.shortcuts import redirect, render
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from apps.core.decorators import membership_required

from . import selectors, services
from .models import Kind, Notification


@membership_required
def notification_list_view(request):
    page = Paginator(selectors.notification_list(membership=request.membership), 20).get_page(
        request.GET.get("page")
    )
    return render(request, "notifications/list.html", {"page": page})


@membership_required
def notification_badge_view(request):
    """Fragmento de la campana (htmx al cargar y cada 60 s)."""
    count = selectors.unread_count(membership=request.membership)
    return render(request, "notifications/_badge.html", {"unread_count": count})


@require_POST
@membership_required
def notification_open_view(request, pk):
    try:
        notification = services.mark_read(membership=request.membership, notification_id=pk)
    except Notification.DoesNotExist as error:
        raise Http404 from error
    target = notification.url
    if target and url_has_allowed_host_and_scheme(target, allowed_hosts={request.get_host()}):
        return redirect(target)
    return redirect("notification_list")


@require_POST
@membership_required
def notification_read_all_view(request):
    services.mark_all_read(membership=request.membership)
    messages.success(request, _("Todas las notificaciones quedaron leídas."))
    return redirect("notification_list")


@membership_required
def notification_preferences_view(request):
    if request.method == "POST":
        values = {
            kind: (request.POST.get(f"{kind}-in_app") == "on", request.POST.get(f"{kind}-email") == "on")
            for kind in Kind.values
        }
        services.save_preferences(membership=request.membership, values=values)
        messages.success(request, _("Preferencias de notificación guardadas."))
        return redirect("notification_preferences")
    rows = selectors.preference_rows(membership=request.membership)
    return render(request, "notifications/preferences.html", {"rows": rows})
