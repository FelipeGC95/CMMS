import uuid

from django import forms
from django.utils.translation import gettext_lazy as _

from .models import AssetClass, AttributeType


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
