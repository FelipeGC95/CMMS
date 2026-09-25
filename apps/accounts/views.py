from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect
from django.views.decorators.http import require_POST

from .services import switch_organization as switch_organization_service


@login_required
@require_POST
def switch_organization(request):
    switch_organization_service(request=request, organization_id=request.POST.get("organization"))
    # Siempre al inicio: la página anterior es de la otra organización.
    return redirect("dashboard")
