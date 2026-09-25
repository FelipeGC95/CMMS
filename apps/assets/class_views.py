"""Clases de activo, sus atributos (AST-01) y los valores en cada activo."""

from django import forms
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext as _

from apps.accounts.permissions import Perm
from apps.core.decorators import membership_required

from . import attributes as attribute_values
from . import services
from .forms import AssetClassForm, AttributeEditForm, AttributeForm
from .models import AssetClass, AssetClassAttribute
from .selectors import active_attributes, asset_class_list, visible_assets


def _class_values(form):
    data = form.cleaned_data
    return {k: data[k] for k in ("code", "name", "iso_category", "level_hint")}


@membership_required(perm=Perm.ASSET_MANAGE)
def asset_class_list_view(request):
    form = AssetClassForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            asset_class = services.create_asset_class(membership=request.membership, **_class_values(form))
        except ValidationError as error:
            form.add_error(None, error)
        else:
            messages.success(request, _("Clase creada: agrega sus atributos."))
            return redirect("asset_class_detail", pk=asset_class.pk)
    page = Paginator(asset_class_list(), 25).get_page(request.GET.get("page"))
    return render(request, "assets/class_list.html", {"page": page, "form": form})


@membership_required(perm=Perm.ASSET_MANAGE)
def asset_class_detail_view(request, pk):
    asset_class = get_object_or_404(AssetClass, pk=pk)
    action = request.POST.get("action") if request.method == "POST" else None
    class_form = AssetClassForm(
        request.POST if action == "class" else None,
        initial={
            "code": asset_class.code,
            "name": asset_class.name,
            "iso_category": asset_class.iso_category,
            "level_hint": asset_class.level_hint,
            "is_active": asset_class.is_active,
        },
    )
    attribute_form = AttributeForm(request.POST if action == "attribute" else None)
    try:
        if action == "class" and class_form.is_valid():
            services.update_asset_class(
                membership=request.membership,
                asset_class=asset_class,
                is_active=class_form.cleaned_data["is_active"],
                **_class_values(class_form),
            )
            messages.success(request, _("Cambios guardados."))
            return redirect("asset_class_detail", pk=pk)
        if action == "attribute" and attribute_form.is_valid():
            services.add_class_attribute(
                membership=request.membership, asset_class=asset_class, **attribute_form.cleaned_data
            )
            messages.success(request, _("Atributo agregado."))
            return redirect("asset_class_detail", pk=pk)
    except ValidationError as error:
        (class_form if action == "class" else attribute_form).add_error(None, error)
    return render(
        request,
        "assets/class_detail.html",
        {
            "asset_class": asset_class,
            "class_form": class_form,
            "attribute_form": attribute_form,
            "attributes": asset_class.attributes.order_by("-is_active", "order", "label"),
        },
    )


@membership_required(perm=Perm.ASSET_MANAGE)
def attribute_edit_view(request, pk, attribute_pk):
    attribute = get_object_or_404(
        AssetClassAttribute.objects.select_related("asset_class"), pk=attribute_pk, asset_class_id=pk
    )
    form = AttributeEditForm(
        request.POST or None,
        initial={
            "label": attribute.label,
            "unit": attribute.unit,
            "choices": "\n".join(attribute.choices),
            "required": attribute.required,
            "order": attribute.order,
            "is_active": attribute.is_active,
        },
    )
    if request.method == "POST" and form.is_valid():
        try:
            services.update_class_attribute(
                membership=request.membership, attribute=attribute, **form.cleaned_data
            )
        except ValidationError as error:
            form.add_error(None, error)
        else:
            messages.success(request, _("Atributo actualizado."))
            return redirect("asset_class_detail", pk=pk)
    return render(request, "assets/attribute_form.html", {"form": form, "attribute": attribute})


@membership_required(perm=Perm.ASSET_MANAGE)
def asset_attributes_view(request, pk):
    asset = get_object_or_404(
        visible_assets(membership=request.membership).select_related("asset_class"), pk=pk
    )
    attributes = active_attributes(asset.asset_class)
    form = forms.Form(
        request.POST or None, initial=attribute_values.initial_values(attributes, asset.attributes)
    )
    form.fields.update(attribute_values.build_form_fields(attributes))
    if request.method == "POST" and form.is_valid():
        values = {a.key: form.cleaned_data.get(attribute_values.field_name(a)) for a in attributes}
        try:
            services.set_asset_attributes(membership=request.membership, asset=asset, values=values)
        except ValidationError as error:
            form.add_error(None, error)
        else:
            messages.success(request, _("Datos técnicos guardados."))
            return redirect("asset_detail", pk=pk)
    return render(request, "assets/asset_attributes_form.html", {"form": form, "asset": asset})
