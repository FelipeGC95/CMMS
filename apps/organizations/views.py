from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from apps.accounts.permissions import Perm
from apps.accounts.scoping import is_scoped
from apps.core.decorators import membership_required
from apps.core.files import private_file_response

from . import services
from .forms import (
    LocationForm,
    OrganizationLogoForm,
    OrganizationSettingsForm,
    SecurityForm,
    WorkOrderStatusLabelsForm,
)
from .models import Location
from .selectors import descendant_ids, location_tree, organization_quotas, visible_locations


def _form_values(form):
    data = form.cleaned_data
    return {
        "code": data["code"],
        "name": data["name"],
        "kind": data["kind"],
        "parent": data["parent"],
        "timezone": data["timezone"],
    }


@membership_required(perm=Perm.ASSET_MANAGE)
def location_list_view(request):
    membership = request.membership
    parents = visible_locations(membership=membership).filter(is_active=True).order_by("code")
    form = LocationForm(request.POST or None, parents=parents, parent_required=is_scoped(membership))
    if request.method == "POST" and form.is_valid():
        try:
            location = services.create_location(membership=membership, **_form_values(form))
        except ValidationError as error:
            form.add_error(None, error)
        else:
            messages.success(request, _("Ubicación %(name)s creada.") % {"name": location.name})
            return redirect("location_list")
    page = Paginator(location_tree(membership=membership), 100).get_page(request.GET.get("page"))
    return render(request, "organizations/location_list.html", {"page": page, "form": form})


@membership_required(perm=Perm.ASSET_MANAGE)
def location_edit_view(request, pk):
    membership = request.membership
    location = get_object_or_404(visible_locations(membership=membership), pk=pk)
    # No se puede mover dentro de sí misma ni de sus descendientes.
    excluded = {location.pk, *descendant_ids(location)}
    parents = visible_locations(membership=membership).exclude(pk__in=excluded).order_by("code")
    initial = {
        "code": location.code,
        "name": location.name,
        "kind": location.kind,
        "parent": location.parent_id,
        "timezone": location.timezone,
        "is_active": location.is_active,
    }
    # Con alcance, la raíz de su subárbol tiene un padre fuera de alcance: no la puede mover.
    parent_locked = location.parent_id is not None and not parents.filter(pk=location.parent_id).exists()
    if parent_locked:
        parents = Location.objects.filter(pk=location.parent_id)
    form = LocationForm(
        request.POST or None,
        initial=initial,
        parents=parents,
        parent_required=is_scoped(membership) and location.parent_id is not None,
    )
    form.fields["parent"].disabled = parent_locked
    if request.method == "POST" and form.is_valid():
        try:
            services.update_location(
                membership=membership,
                location=location,
                is_active=form.cleaned_data["is_active"],
                **_form_values(form),
            )
        except ValidationError as error:
            form.add_error(None, error)
        else:
            messages.success(request, _("Cambios guardados."))
            return redirect("location_list")
    return render(request, "organizations/location_form.html", {"form": form, "location": location})


@membership_required(perm=Perm.ASSET_MANAGE)
@require_POST
def location_delete_view(request, pk):
    location = get_object_or_404(visible_locations(membership=request.membership), pk=pk)
    try:
        services.delete_location(membership=request.membership, location=location)
    except ValidationError as error:
        messages.error(request, " ".join(error.messages))
        return redirect("location_edit", pk=pk)
    messages.success(request, _("Ubicación eliminada."))
    return redirect("location_list")


@membership_required(perm=Perm.ORGANIZATION_MANAGE)
def organization_settings_view(request):
    """Configuración de la organización para su administrador (WO-02 CA2, REQ-03 CA2)."""
    membership = request.membership
    organization = membership.organization
    # Dos formularios en la página; el botón de envío indica cuál se guarda.
    section = request.POST.get("section", "closing") if request.method == "POST" else None
    initial = {field: getattr(organization, field) for field in services.SETTINGS_FIELDS}
    form = OrganizationSettingsForm(request.POST if section == "closing" else None, initial=initial)
    labels_form = WorkOrderStatusLabelsForm(
        request.POST if section == "status_labels" else None,
        initial=organization.work_order_status_labels or {},
        choices=services.work_order_status_choices(),
        prefix="label",
    )
    if section == "closing" and form.is_valid():
        services.update_organization_settings(membership=membership, **form.cleaned_data)
        messages.success(request, _("Configuración guardada."))
        return redirect("organization_settings")
    security_form = SecurityForm(
        request.POST if section == "security" else None, initial={"mfa_policy": organization.mfa_policy}
    )
    if section == "security" and security_form.is_valid():
        services.update_mfa_policy(membership=membership, **security_form.cleaned_data)
        messages.success(request, _("Configuración de seguridad guardada."))
        return redirect("organization_settings")
    logo_form = OrganizationLogoForm(
        request.POST if section == "logo" else None, request.FILES if section == "logo" else None
    )
    if not organization.logo:
        del logo_form.fields["remove"]
    if section == "logo" and logo_form.is_valid():
        try:
            if logo_form.cleaned_data.get("logo"):
                services.set_organization_logo(
                    membership=membership, uploaded_file=logo_form.cleaned_data["logo"]
                )
                messages.success(request, _("Logo guardado."))
            else:
                services.remove_organization_logo(membership=membership)
                messages.success(request, _("Logo quitado."))
        except ValidationError as error:
            logo_form.add_error("logo", error)
        else:
            return redirect("organization_settings")
    if section == "status_labels" and labels_form.is_valid():
        try:
            services.update_work_order_status_labels(membership=membership, labels=labels_form.cleaned_data)
        except ValidationError as error:
            labels_form.add_error(None, error)
        else:
            messages.success(request, _("Nombres de los estados guardados."))
            return redirect("organization_settings")
    return render(
        request,
        "organizations/settings.html",
        {
            "form": form,
            "labels_form": labels_form,
            "logo_form": logo_form,
            "security_form": security_form,
            "quotas": organization_quotas(membership=membership),
        },
    )


@membership_required
def organization_logo_view(request):
    """Logo de la organización activa, solo con sesión (sin URL pública, ADR-014)."""
    logo = request.organization.logo
    if not logo:
        raise Http404
    return private_file_response(logo, content_type="image/png")
