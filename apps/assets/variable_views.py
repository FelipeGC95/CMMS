"""Variables de condición desde la ficha del activo (CBM-01 CA3/CA5): alta, edición y
desactivación con `asset.manage`. Vistas delgadas sobre `assets.services`."""

from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from apps.accounts.permissions import Perm, PermissionDeniedError
from apps.core.decorators import membership_required

from .forms import VariableForm, apply_service_errors
from .models import VARIABLE_KINDS, Meter
from .selectors import visible_assets
from .services import create_variable, set_variable_active, update_variable


def _asset_anchor(meter_or_asset_pk, anchor="meters"):
    return f"{reverse('asset_detail', args=[meter_or_asset_pk])}#{anchor}"


def _variable(request, pk, meter_pk):
    """Variable de un activo visible para la membresía (404 si es de otra organización o sitio)."""
    return get_object_or_404(
        Meter.objects.select_related("asset").filter(
            asset__in=visible_assets(membership=request.membership), kind__in=VARIABLE_KINDS
        ),
        pk=meter_pk,
        asset_id=pk,
    )


@membership_required(perm=Perm.ASSET_MANAGE)
def variable_create_view(request, pk):
    asset = get_object_or_404(visible_assets(membership=request.membership), pk=pk, is_active=True)
    form = VariableForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            meter = create_variable(membership=request.membership, asset=asset, **form.cleaned_data)
        except PermissionDeniedError as exc:
            raise PermissionDenied from exc
        except ValidationError as error:
            apply_service_errors(form, error)
        else:
            messages.success(
                request, _("Variable agregada: %(name)s.") % {"name": meter.name or meter.get_kind_display()}
            )
            return redirect(_asset_anchor(asset.pk, f"meter-{meter.pk}"))
    return render(request, "assets/variable_form.html", {"form": form, "asset": asset, "meter": None})


@membership_required(perm=Perm.ASSET_MANAGE)
def variable_edit_view(request, pk, meter_pk):
    meter = _variable(request, pk, meter_pk)
    form = VariableForm(request.POST or None, meter=meter, initial={"name": meter.name, "unit": meter.unit})
    if request.method == "POST" and form.is_valid():
        try:
            update_variable(membership=request.membership, meter=meter, **form.cleaned_data)
        except PermissionDeniedError as exc:
            raise PermissionDenied from exc
        except ValidationError as error:
            apply_service_errors(form, error)
        else:
            messages.success(request, _("Cambios guardados."))
            return redirect(_asset_anchor(meter.asset_id, f"meter-{meter.pk}"))
    return render(request, "assets/variable_form.html", {"form": form, "asset": meter.asset, "meter": meter})


@require_POST
@membership_required(perm=Perm.ASSET_MANAGE)
def variable_active_view(request, pk, meter_pk):
    """Desactiva o reactiva una variable (no se borra: su historial y alertas siguen)."""
    meter = _variable(request, pk, meter_pk)
    is_active = request.POST.get("is_active") == "1"
    try:
        set_variable_active(membership=request.membership, meter=meter, is_active=is_active)
    except PermissionDeniedError as exc:
        raise PermissionDenied from exc
    except ValidationError as error:
        messages.error(request, " ".join(error.messages))
    else:
        if is_active:
            messages.success(request, _("Variable reactivada."))
        else:
            messages.success(request, _("Variable desactivada: su historial y sus alertas se conservan."))
    return redirect(_asset_anchor(meter.asset_id, f"meter-{meter.pk}"))
