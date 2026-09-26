import uuid

from django import forms
from django.core.exceptions import ValidationError
from django.db.models import Q
from django.urls import reverse
from django.utils.translation import gettext_lazy as _

from apps.accounts.scoping import allowed_location_ids
from apps.attachments.models import Purpose
from apps.organizations.models import Location

from .attributes import build_form_fields, field_name, initial_values
from .models import MANUAL_STATUSES, Asset, AssetClass, AttributeType
from .selectors import active_attributes, asset_form_choices


class AssetAttachmentForm(forms.Form):
    # La firma (Purpose.SIGNATURE) solo nace al completar una OT, no se sube a mano.
    purpose = forms.ChoiceField(
        label=_("Tipo"),
        choices=[(p.value, p.label) for p in (Purpose.PHOTO, Purpose.DOCUMENT)],
        initial=Purpose.DOCUMENT,
    )
    file = forms.FileField(label=_("Archivo"), help_text=_("Fotos o documentos, hasta 20 MB."))
    # Generado al mostrar el formulario: un doble envío no duplica el adjunto.
    client_uuid = forms.UUIDField(widget=forms.HiddenInput, required=False, initial=uuid.uuid4)


class MeterReadingForm(forms.Form):
    value = forms.DecimalField(label=_("Lectura actual"), min_value=0, max_digits=14, decimal_places=3)
    is_reset = forms.BooleanField(
        label=_("Se cambió o reinició el medidor"),
        required=False,
        help_text=_("Márcalo solo si el medidor volvió a cero o se reemplazó."),
    )
    # Generado al mostrar el formulario: un doble envío no registra dos lecturas.
    client_uuid = forms.UUIDField(widget=forms.HiddenInput, required=False, initial=uuid.uuid4)


class AssetClassForm(forms.Form):
    code = forms.CharField(label=_("Código"), max_length=30)
    name = forms.CharField(label=_("Nombre"), max_length=120)
    iso_category = forms.ChoiceField(label=_("Categoría ISO 14224"), choices=AssetClass.IsoCategory.choices)
    level_hint = forms.ChoiceField(label=_("Nivel típico"), choices=AssetClass.Level.choices)
    is_active = forms.BooleanField(label=_("Activa"), required=False, initial=True)


def _split_choices(text):
    return [line for line in (text or "").splitlines() if line.strip()]


class AttributeForm(forms.Form):
    label = forms.CharField(label=_("Nombre"), max_length=80)
    data_type = forms.ChoiceField(label=_("Tipo"), choices=AttributeType.choices)
    unit = forms.CharField(label=_("Unidad"), max_length=20, required=False, help_text=_("Ej.: kg, kW, bar."))
    choices = forms.CharField(
        label=_("Opciones de la lista"),
        required=False,
        widget=forms.Textarea(attrs={"rows": 3}),
        help_text=_("Solo para el tipo lista: una opción por línea."),
    )
    required = forms.BooleanField(label=_("Obligatorio"), required=False)

    def clean_choices(self):
        return _split_choices(self.cleaned_data["choices"])


class AttributeEditForm(forms.Form):
    label = forms.CharField(label=_("Nombre"), max_length=80)
    unit = forms.CharField(label=_("Unidad"), max_length=20, required=False)
    choices = forms.CharField(
        label=_("Opciones de la lista"),
        required=False,
        widget=forms.Textarea(attrs={"rows": 3}),
        help_text=_("Una opción por línea. Quitar una opción no borra los valores ya guardados."),
    )
    required = forms.BooleanField(label=_("Obligatorio"), required=False)
    order = forms.IntegerField(label=_("Orden"), min_value=0, max_value=999)
    is_active = forms.BooleanField(
        label=_("Visible"),
        required=False,
        help_text=_("Oculto: no se muestra ni se pide, pero sus valores se conservan."),
    )

    def clean_choices(self):
        return _split_choices(self.cleaned_data["choices"])


class AssetForm(forms.Form):
    """Alta y edición de activo (AST-02). Alta rápida: solo código, nombre y clase (CA3).

    Los campos de los atributos de la clase elegida se agregan al construir el
    formulario; al cambiar la clase, htmx recarga esa sección
    (`asset_attribute_fields_view`).
    """

    code = forms.CharField(label=_("Código"), max_length=30)
    name = forms.CharField(label=_("Nombre"), max_length=150)
    asset_class = forms.ModelChoiceField(label=_("Clase"), queryset=AssetClass.all_tenants.none())
    criticality = forms.ChoiceField(
        label=_("Criticidad"), choices=Asset.Criticality.choices, initial=Asset.Criticality.B
    )
    location = forms.ModelChoiceField(
        label=_("Ubicación"),
        queryset=Location.all_tenants.none(),
        required=False,
        empty_label=_("Sin ubicación"),
    )
    parent = forms.ModelChoiceField(
        label=_("Parte de (activo padre)"),
        queryset=Asset.all_tenants.none(),
        required=False,
        empty_label=_("— (activo principal)"),
        help_text=_("Para registrar un componente dentro de un equipo o sistema."),
    )
    manufacturer = forms.CharField(label=_("Fabricante"), max_length=100, required=False)
    model = forms.CharField(label=_("Modelo"), max_length=100, required=False)
    serial_number = forms.CharField(label=_("N.º de serie"), max_length=100, required=False)
    year = forms.IntegerField(label=_("Año de fabricación"), min_value=1900, max_value=2100, required=False)
    in_service_on = forms.DateField(
        label=_("Fecha de puesta en servicio"),
        required=False,
        widget=forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
    )

    BASIC_FIELDS = ("code", "name", "asset_class", "criticality")
    PLACE_FIELDS = ("location", "parent")
    MAKE_FIELDS = ("manufacturer", "model", "serial_number", "year", "in_service_on")

    def __init__(self, *args, membership, asset=None, **kwargs):
        if asset is not None and "initial" not in kwargs:
            kwargs["initial"] = {
                **{name: getattr(asset, name) for name in self.MAKE_FIELDS},
                "code": asset.code,
                "name": asset.name,
                "asset_class": asset.asset_class_id,
                "criticality": asset.criticality,
                "location": asset.location_id,
                "parent": asset.parent_id,
            }
        super().__init__(*args, **kwargs)
        self.asset = asset
        choices = asset_form_choices(membership=membership)
        classes, locations, parents = choices["classes"], choices["locations"], choices["parents"]
        if asset is not None:
            # La ficha conserva sus referencias aunque se hayan desactivado; nunca se ofrece a sí misma.
            classes = AssetClass.objects.filter(Q(is_active=True) | Q(pk=asset.asset_class_id))
            if asset.location_id and allowed_location_ids(membership) is None:
                locations = Location.objects.filter(Q(is_active=True) | Q(pk=asset.location_id))
            parents = parents.exclude(pk=asset.pk)
        self.fields["asset_class"].queryset = classes.order_by("name")
        self.fields["location"].queryset = locations.order_by("name")
        self.fields["parent"].queryset = parents.order_by("code")
        if allowed_location_ids(membership) is not None:
            self.fields["location"].required = True
            self.fields["location"].empty_label = None
        url = reverse("asset_attribute_fields")
        if asset is not None:
            url = f"{url}?asset={asset.pk}"
        self.fields["asset_class"].widget.attrs.update(
            {"hx-get": url, "hx-trigger": "change", "hx-target": "#asset-attributes", "hx-include": "this"}
        )

        self.class_chosen = False
        self.attributes = self._class_attributes()
        attribute_fields = build_form_fields(self.attributes)
        if asset is None:
            # Alta rápida (CA3): los atributos se completan después si hace falta.
            for field in attribute_fields.values():
                field.required = False
        self.fields.update(attribute_fields)
        if asset is not None and not self.is_bound:
            self.initial.update(initial_values(self.attributes, asset.attributes))

    def _class_attributes(self):
        if self.is_bound:
            class_id = self.data.get(self.add_prefix("asset_class"))
        else:
            class_id = self.initial.get("asset_class")
        if not class_id:
            return []
        try:
            asset_class = self.fields["asset_class"].queryset.filter(pk=class_id).first()
        except (ValueError, ValidationError):
            return []
        self.class_chosen = asset_class is not None
        return active_attributes(asset_class) if asset_class else []

    def _bound(self, names):
        return [self[name] for name in names]

    def basic_fields(self):
        return self._bound(self.BASIC_FIELDS)

    def place_fields(self):
        return self._bound(self.PLACE_FIELDS)

    def make_fields(self):
        return self._bound(self.MAKE_FIELDS)

    def attribute_fields(self):
        return [self[field_name(attribute)] for attribute in self.attributes]

    def service_kwargs(self) -> dict:
        data = self.cleaned_data
        kwargs = {name: data.get(name) for name in self.BASIC_FIELDS + self.PLACE_FIELDS + self.MAKE_FIELDS}
        for name in ("manufacturer", "model", "serial_number"):
            kwargs[name] = kwargs[name] or ""
        kwargs["attributes"] = {a.key: data.get(field_name(a)) for a in self.attributes}
        return kwargs


class AssetStatusForm(forms.Form):
    """Cambio manual de estado (AST-04): ofrece los estados manuales distintos del actual."""

    status = forms.ChoiceField(label=_("Nuevo estado"))
    reason = forms.CharField(
        label=_("Motivo"),
        max_length=200,
        widget=forms.Textarea(attrs={"rows": 3}),
        help_text=_("Queda en el historial de estados del activo."),
    )

    def __init__(self, *args, asset, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["status"].choices = [
            (value, label)
            for value, label in Asset.Status.choices
            if value in MANUAL_STATUSES and value != asset.status
        ]


class AssetDecommissionForm(forms.Form):
    reason = forms.CharField(
        label=_("Motivo de la baja"),
        max_length=200,
        widget=forms.Textarea(attrs={"rows": 3}),
        help_text=_("Por ejemplo: vendido, reemplazado o fuera de uso."),
    )


class _AssignmentChoiceField(forms.ModelMultipleChoiceField):
    def label_from_instance(self, obj):
        return _("%(plan)s · cada %(frequency)s") % {
            "plan": obj.plan.name,
            "frequency": obj.plan.frequency_label,
        }


class AssetReactivateForm(forms.Form):
    """Reactivar un activo dado de baja (AST-04): motivo y planes que vuelven a activarse."""

    reason = forms.CharField(
        label=_("Motivo de la reactivación"),
        max_length=200,
        widget=forms.Textarea(attrs={"rows": 3}),
        help_text=_("Por ejemplo: reparado, recuperado o vuelve a producción."),
    )
    assignments = _AssignmentChoiceField(
        label=_("Planes preventivos que se reactivan"),
        queryset=None,
        required=False,
        widget=forms.CheckboxSelectMultiple,
        help_text=_("Su próximo vencimiento se calcula desde hoy. Los no marcados siguen desactivados."),
    )

    def __init__(self, *args, assignments, **kwargs):
        super().__init__(*args, **kwargs)
        if assignments:
            self.fields["assignments"].queryset = assignments
        else:
            del self.fields["assignments"]

    def assignment_ids(self):
        return [a.pk for a in self.cleaned_data.get("assignments", [])]
