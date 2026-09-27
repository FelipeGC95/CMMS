"""Importación de lecturas por CSV (AST-09): subir → vista previa → confirmar."""

import secrets

from django import forms
from django.contrib import messages
from django.shortcuts import redirect, render
from django.utils.translation import gettext, ngettext
from django.utils.translation import gettext_lazy as _
from django.views.decorators.http import require_POST

from apps.accounts.permissions import Perm
from apps.core.decorators import membership_required

from .models import MeterReading
from .reading_imports import (
    MAX_ROWS,
    ReadingImportFileError,
    apply_reading_import,
    build_reading_preview,
    parse_csv,
)

SESSION_PREFIX = "reading_import:"


class ReadingImportForm(forms.Form):
    file = forms.FileField(
        label=_("Archivo CSV"),
        widget=forms.ClearableFileInput(attrs={"accept": ".csv,text/csv"}),
    )


def _clear_pending(session):
    for key in [k for k in session.keys() if k.startswith(SESSION_PREFIX)]:
        del session[key]


@membership_required(perm=Perm.ASSET_MANAGE)
def reading_import_view(request):
    """Paso 1: subida. Al subir, valida y muestra la vista previa (sin guardar)."""
    form = ReadingImportForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        try:
            preview = build_reading_preview(
                parse_csv(form.cleaned_data["file"]), membership=request.membership
            )
        except ReadingImportFileError as exc:
            form.add_error("file", " ".join(exc.messages))
        else:
            _clear_pending(request.session)
            token = secrets.token_urlsafe(8)
            request.session[SESSION_PREFIX + token] = {
                "organization": str(request.organization.id),
                **preview.to_session(),
            }
            counts = {action: preview.count(action) for action in ("create", "duplicate", "error")}
            return render(
                request,
                "assets/reading_import_preview.html",
                {"preview": preview, "token": token, "counts": counts},
            )
    return render(request, "assets/reading_import.html", {"form": form, "max_rows": MAX_ROWS})


@require_POST
@membership_required(perm=Perm.ASSET_MANAGE)
def reading_import_confirm_view(request, token):
    """Paso 2: guarda las filas válidas de la vista previa guardada en la sesión."""
    pending = request.session.pop(SESSION_PREFIX + token, None)
    if pending is None or pending["organization"] != str(request.organization.id):
        messages.error(
            request, gettext("La vista previa expiró o es de otra organización. Sube el archivo de nuevo.")
        )
        return redirect("reading_import")
    result = apply_reading_import(
        pending["rows"], membership=request.membership, source=MeterReading.Source.IMPORT
    )
    summary = ngettext("%(n)s lectura registrada", "%(n)s lecturas registradas", result.created) % {
        "n": result.created
    }
    if result.duplicates:
        summary += ", " + ngettext("%(n)s ya existía", "%(n)s ya existían", result.duplicates) % {
            "n": result.duplicates
        }
    messages.success(request, gettext("Importación lista: %(summary)s.") % {"summary": summary})
    for row, error in result.errors[:10]:
        messages.error(request, gettext("Fila %(row)s: %(error)s") % {"row": row, "error": error})
    for warning in result.warnings[:10]:
        messages.warning(request, warning)
    return redirect("asset_list")
