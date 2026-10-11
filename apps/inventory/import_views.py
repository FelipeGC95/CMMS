"""Importación de repuestos y stock (INV-02): subir → vista previa → confirmar (todo o nada)."""

import secrets

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.utils.translation import gettext, ngettext
from django.views.decorators.http import require_POST

from apps.accounts.permissions import Perm
from apps.core.decorators import membership_required

from .forms import PartImportForm
from .imports import (
    MAX_ROWS,
    ImportFileError,
    apply_import,
    build_preview,
    build_template,
    parse_file,
    template_filename,
)

SESSION_PREFIX = "part_import:"


def _clear_pending(session):
    for key in [k for k in session.keys() if k.startswith(SESSION_PREFIX)]:
        del session[key]


@membership_required(perm=Perm.INVENTORY_MANAGE)
def part_import_view(request):
    """Paso 1: instrucciones y subida. Al subir, valida y muestra la vista previa (sin guardar)."""
    form = PartImportForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        try:
            preview = build_preview(parse_file(form.cleaned_data["file"]), membership=request.membership)
        except ImportFileError as exc:
            form.add_error("file", " ".join(exc.messages))
        else:
            _clear_pending(request.session)
            token = ""
            if not preview.has_errors:
                token = secrets.token_urlsafe(8)
                request.session[SESSION_PREFIX + token] = {
                    "organization": str(request.organization.id),
                    **preview.to_session(),
                }
            counts = {action: preview.count(action) for action in ("create", "update", "error")}
            return render(
                request,
                "inventory/import_preview.html",
                {"preview": preview, "token": token, "counts": counts, "stock_counts": preview.counts},
            )
    return render(request, "inventory/import.html", {"form": form, "max_rows": MAX_ROWS})


@require_POST
@membership_required(perm=Perm.INVENTORY_MANAGE)
def part_import_confirm_view(request, token):
    """Paso 2: guarda todas las filas de la vista previa guardada en la sesión, en una transacción."""
    pending = request.session.pop(SESSION_PREFIX + token, None)
    if pending is None or pending["organization"] != str(request.organization.id):
        messages.error(
            request, gettext("La vista previa expiró o es de otra organización. Sube el archivo de nuevo.")
        )
        return redirect("inventory_import")
    try:
        result = apply_import(pending, membership=request.membership)
    except ValidationError as error:
        messages.error(
            request,
            gettext("No se importó nada: %(reason)s") % {"reason": " ".join(error.messages)},
        )
        return redirect("inventory_import")
    summary = [
        ngettext("%(n)s repuesto creado", "%(n)s repuestos creados", result.created) % {"n": result.created},
        ngettext("%(n)s actualizado", "%(n)s actualizados", result.updated) % {"n": result.updated},
        ngettext("%(n)s ajuste de stock", "%(n)s ajustes de stock", result.adjusted) % {"n": result.adjusted},
    ]
    if result.unchanged:
        summary.append(
            ngettext("%(n)s conteo sin cambios", "%(n)s conteos sin cambios", result.unchanged)
            % {"n": result.unchanged}
        )
    messages.success(request, gettext("Importación lista: %(summary)s.") % {"summary": ", ".join(summary)})
    return redirect("inventory_home")


@membership_required(perm=Perm.INVENTORY_MANAGE)
def part_import_template_view(request):
    response = HttpResponse(
        build_template(membership=request.membership),
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    response["Content-Disposition"] = f'attachment; filename="{template_filename()}"'
    return response
