"""Inventario desde los planes (INV-11): «Crear catálogo desde los planes» y el buscador
de repuestos que usan el borrador de plan y el enlace en versiones publicadas.

Viven bajo /inventory/from-plans/: sin el módulo `inventory` el middleware de
módulos responde 403 con el mensaje de plan.
"""

import re
import uuid

from django.contrib import messages
from django.core.paginator import Paginator
from django.db.models import Q
from django.shortcuts import redirect, render
from django.utils.translation import gettext as _
from django.utils.translation import ngettext
from django.views.decorators.http import require_GET, require_POST

from apps.accounts.permissions import Perm
from apps.core.decorators import membership_required

from .from_plans import create_parts_from_plan_resources, plan_part_proposals
from .models import Part

SEARCH_LIMIT = 20
FIELD_NAME = re.compile(r"^[a-z][a-z0-9_-]{0,39}$")


@require_GET
@membership_required(perm=Perm.PLAN_MANAGE)
def part_search_view(request):
    """Opciones del buscador de repuestos (htmx): activos que coinciden por código, nombre o n° de parte."""
    q = (request.GET.get("part_q") or request.GET.get("q", "")).strip()[:100]
    name = request.GET.get("name", "part")
    if not FIELD_NAME.match(name):
        name = "part"
    parts = Part.objects.filter(is_active=True)
    if q:
        parts = parts.filter(Q(code__icontains=q) | Q(name__icontains=q) | Q(manufacturer_ref__icontains=q))
    try:  # el repuesto ya elegido sigue marcado aunque no coincida con la búsqueda
        selected = Part.objects.filter(pk=uuid.UUID(request.GET.get("selected", ""))).first()
    except ValueError:
        selected = None
    return render(
        request,
        "inventory/_plan_part_options.html",
        {
            "parts": list(parts.order_by("code")[:SEARCH_LIMIT]),
            "selected": selected,
            "name": name,
            "q": q,
            "allow_empty": request.GET.get("empty") == "1",
            "searched": True,
        },
    )


@membership_required(perm=Perm.INVENTORY_MANAGE)
def from_plans_view(request):
    """Vista previa: repuestos que se crearían desde los recursos de los planes (INV-11 CA3)."""
    proposals = plan_part_proposals(membership=request.membership)
    page = Paginator(proposals, 50).get_page(request.GET.get("page"))
    return render(
        request,
        "inventory/from_plans.html",
        {
            "page": page,
            "new_count": sum(1 for p in proposals if p.existing is None),
            "existing_count": sum(1 for p in proposals if p.existing is not None),
            "resource_count": sum(p.resource_count for p in proposals),
        },
    )


@require_POST
@membership_required(perm=Perm.INVENTORY_MANAGE)
def from_plans_confirm_view(request):
    created, linked = create_parts_from_plan_resources(membership=request.membership)
    if linked:
        messages.success(
            request,
            ngettext("%(created)s repuesto creado; ", "%(created)s repuestos creados; ", created)
            % {"created": created}
            + ngettext(
                "%(linked)s recurso de plan enlazado al catálogo.",
                "%(linked)s recursos de plan enlazados al catálogo.",
                linked,
            )
            % {"linked": linked},
        )
    else:
        messages.info(request, _("No había repuestos de planes sin enlazar."))
    return redirect("inventory_from_plans")
