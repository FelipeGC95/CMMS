"""«Cargar plantilla de rubro» (ORG-03 CA3, PM-07): elegir rubro → ver qué se crea → confirmar."""

from django.contrib import messages
from django.http import Http404
from django.shortcuts import redirect, render
from django.utils.translation import gettext as _
from django.utils.translation import ngettext

from apps.accounts.permissions import Perm
from apps.core.decorators import membership_required
from apps.work.models import FailureCode

from .industry_templates import apply_industry_template, preview_industry_template
from .templates_library import INDUSTRIES, available_templates

manager_required = membership_required(perm=Perm.PLAN_MANAGE)


@manager_required
def industry_template_list_view(request):
    return render(request, "maintenance/industry_templates.html", {"templates": available_templates()})


@manager_required
def industry_template_view(request, industry):
    """GET: vista previa (sin escribir). POST: copia a la organización lo que aún no tiene."""
    if industry not in INDUSTRIES:
        raise Http404
    if request.method == "POST":
        summary = apply_industry_template(
            organization=request.organization, industry=industry, by=request.user
        )
        if summary.is_empty:
            messages.info(request, _("Tu organización ya tenía todo lo de esta plantilla: no se creó nada."))
        else:
            parts = [
                ngettext("%(n)s clase de activo", "%(n)s clases de activo", len(summary.classes)),
                ngettext("%(n)s código de falla", "%(n)s códigos de falla", len(summary.failure_codes)),
                ngettext("%(n)s plan preventivo", "%(n)s planes preventivos", len(summary.plans)),
            ]
            counts = (len(summary.classes), len(summary.failure_codes), len(summary.plans))
            text = ", ".join(part % {"n": n} for part, n in zip(parts, counts, strict=True))
            messages.success(
                request,
                _("Plantilla «%(name)s» cargada: %(summary)s. Revisa y ajusta lo que necesites.")
                % {"name": summary.name, "summary": text},
            )
        return redirect("plan_list")
    summary = preview_industry_template(organization=request.organization, industry=industry)
    failure_groups = [
        (label, [f for f in summary.failure_codes if f["kind"] == kind])
        for kind, label in FailureCode.Kind.choices
    ]
    return render(
        request,
        "maintenance/industry_template_preview.html",
        {"summary": summary, "failure_groups": [(label, rows) for label, rows in failure_groups if rows]},
    )
