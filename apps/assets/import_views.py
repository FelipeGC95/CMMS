"""Importación de activos desde Excel (AST-08): subir → vista previa → confirmar."""

import secrets

from django import forms
from django.contrib import messages
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.translation import gettext, ngettext
from django.utils.translation import gettext_lazy as _
from django.views.decorators.http import require_POST

from apps.accounts.permissions import Perm
from apps.core.decorators import membership_required
from apps.organizations.services import QuotaExceeded, quota_warning

from .imports import MAX_ROWS, ImportFileError, apply_import, build_preview, build_template, parse_workbook

SESSION_PREFIX = "asset_import:"
# URL fija que guarda el asistente de puesta en marcha (nunca viene del usuario).
IMPORT_RETURN_KEY = "asset_import_return"


class AssetImportForm(forms.Form):
    file = forms.FileField(
        label=_("Archivo Excel (.xlsx)"),
        widget=forms.ClearableFileInput(
            attrs={"accept": ".xlsx,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}
        ),
    )


def _clear_pending(session):
    for key in [k for k in session.keys() if k.startswith(SESSION_PREFIX)]:
        del session[key]


@membership_required(perm=Perm.ASSET_MANAGE)
def asset_import_view(request):
    """Paso 1: instrucciones y subida. Al subir, valida y muestra la vista previa (sin guardar)."""
    form = AssetImportForm(request.POST or None, request.FILES or None)
    if request.method == "GET":
        if request.GET.get("from") == "onboarding":
            request.session[IMPORT_RETURN_KEY] = reverse("onboarding_step", args=["assets"])
        else:
            request.session.pop(IMPORT_RETURN_KEY, None)
    if request.method == "GET" and (
        warning := quota_warning(organization=request.organization, resource="assets")
    ):
        messages.warning(request, warning)  # ORG-10 CA1: aviso al 80 % del cupo
    if request.method == "POST" and form.is_valid():
        try:
            preview = build_preview(parse_workbook(form.cleaned_data["file"]), membership=request.membership)
        except ImportFileError as exc:
            form.add_error("file", " ".join(exc.messages))
        else:
            _clear_pending(request.session)
            token = secrets.token_urlsafe(8)
            request.session[SESSION_PREFIX + token] = {
                "organization": str(request.organization.id),
                **preview.to_session(),
            }
            counts = {action: preview.count(action) for action in ("create", "update", "error")}
            return render(
                request, "assets/import_preview.html", {"preview": preview, "token": token, "counts": counts}
            )
    return render(request, "assets/import.html", {"form": form, "max_rows": MAX_ROWS})


@require_POST
@membership_required(perm=Perm.ASSET_MANAGE)
def asset_import_confirm_view(request, token):
    """Paso 2: guarda las filas válidas de la vista previa guardada en la sesión."""
    pending = request.session.pop(SESSION_PREFIX + token, None)
    if pending is None or pending["organization"] != str(request.organization.id):
        messages.error(
            request, gettext("La vista previa expiró o es de otra organización. Sube el archivo de nuevo.")
        )
        return redirect("asset_import")
    try:
        result = apply_import(pending, assign_class_plans=request.POST.get("assign_plans") == "on")
    except QuotaExceeded as error:
        messages.error(request, " ".join(error.messages))
        return redirect("asset_import")
    counts = [
        (result.created, ngettext("%(n)s activo creado", "%(n)s activos creados", result.created), True),
        (result.updated, ngettext("%(n)s actualizado", "%(n)s actualizados", result.updated), True),
        (
            result.classes_created,
            ngettext("%(n)s clase nueva", "%(n)s clases nuevas", result.classes_created),
            False,
        ),
        (
            result.locations_created,
            ngettext("%(n)s ubicación nueva", "%(n)s ubicaciones nuevas", result.locations_created),
            False,
        ),
        (
            result.readings,
            ngettext("%(n)s lectura de medidor", "%(n)s lecturas de medidor", result.readings),
            False,
        ),
        (
            result.plans_assigned,
            ngettext(
                "%(n)s plan preventivo asignado", "%(n)s planes preventivos asignados", result.plans_assigned
            ),
            False,
        ),
    ]
    parts = [text % {"n": n} for n, text, always in counts if always or n]
    messages.success(request, gettext("Importación lista: %(summary)s.") % {"summary": ", ".join(parts)})
    for warning in result.warnings[:10]:
        messages.warning(request, warning)
    # Abierta desde la puesta en marcha (ORG-03): vuelve al paso de activos.
    return redirect(request.session.pop(IMPORT_RETURN_KEY, None) or "asset_list")


@membership_required(perm=Perm.ASSET_MANAGE)
def asset_import_template_view(request):
    from .imports import template_filename

    response = HttpResponse(
        build_template(),
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    response["Content-Disposition"] = f'attachment; filename="{template_filename()}"'
    return response
