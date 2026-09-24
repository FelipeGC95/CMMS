from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect
from django.views.decorators.http import require_POST

from apps.core.middleware import SESSION_KEY


@login_required
@require_POST
def switch_organization(request):
    wanted = request.POST.get("organization")
    if any(str(m.organization_id) == wanted for m in request.memberships):
        request.session.cycle_key()
        request.session[SESSION_KEY] = wanted
    # Siempre al inicio: la página anterior es de la otra organización.
    return redirect("dashboard")
