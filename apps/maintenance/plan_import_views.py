"""Planes por Excel (PM-08): exportar, subir → vista previa → confirmar (como la importación de activos)."""

import secrets

from django import forms
from django.contrib import messages
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.utils.translation import gettext, ngettext
from django.utils.translation import gettext_lazy as _
from django.views.decorators.http import require_POST

from apps.accounts.permissions import Perm
from apps.assets.imports import ImportFileError
from apps.core.decorators import membership_required

from .plan_excel import (
    MAX_PLANS,
    apply_plan_import,
    build_plan_preview,
    build_plans_workbook,
    export_filename,
    parse_plan_workbook,
)

SESSION_PREFIX = "plan_import:"
manager_required = membership_required(perm=Perm.PLAN_MANAGE)


class PlanImportForm(forms.Form):
    file = forms.FileField(
        label=_("Archivo Excel (.xlsx)"),
        widget=forms.ClearableFileInput(
            attrs={"accept": ".xlsx,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}
        ),
    )


@manager_required
def plan_export_view(request):
    response = HttpResponse(
        build_plans_workbook(membership=request.membership),
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    response["Content-Disposition"] = f'attachment; filename="{export_filename()}"'
    return response


@manager_required
def plan_import_view(request):
    form = PlanImportForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        try:
            preview = build_plan_preview(
                parse_plan_workbook(form.cleaned_data["file"]), membership=request.membership
            )
        except ImportFileError as exc:
            form.add_error("file", " ".join(exc.messages))
        else:
            for key in [k for k in request.session.keys() if k.startswith(SESSION_PREFIX)]:
                del request.session[key]
            token = secrets.token_urlsafe(8)
            request.session[SESSION_PREFIX + token] = {
                "organization": str(request.organization.id),
                **preview.to_session(),
            }
            counts = {a: preview.count(a) for a in ("create", "draft", "unchanged", "error")}
            return render(
                request,
                "maintenance/plan_import_preview.html",
                {"preview": preview, "token": token, "counts": counts},
            )
    return render(request, "maintenance/plan_import.html", {"form": form, "max_plans": MAX_PLANS})


@require_POST
@manager_required
def plan_import_confirm_view(request, token):
    pending = request.session.pop(SESSION_PREFIX + token, None)
    if pending is None or pending["organization"] != str(request.organization.id):
        messages.error(
            request, gettext("La vista previa expiró o es de otra organización. Sube el archivo de nuevo.")
        )
        return redirect("plan_import")
    result = apply_plan_import(pending, membership=request.membership)
    parts = [
        ngettext("%(n)s plan creado", "%(n)s planes creados", result.created) % {"n": result.created},
        ngettext("%(n)s borrador nuevo", "%(n)s borradores nuevos", result.drafts) % {"n": result.drafts},
    ]
    messages.success(request, gettext("Importación lista: %(summary)s.") % {"summary": ", ".join(parts)})
    if result.skipped:
        messages.warning(
            request,
            gettext("Se omitieron porque cambiaron mientras revisabas: %(codes)s.")
            % {"codes": ", ".join(result.skipped)},
        )
    return redirect("plan_list")
