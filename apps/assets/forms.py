import uuid

from django import forms
from django.utils.translation import gettext_lazy as _


class MeterReadingForm(forms.Form):
    value = forms.DecimalField(label=_("Lectura actual"), min_value=0, max_digits=14, decimal_places=3)
    is_reset = forms.BooleanField(
        label=_("Se cambió o reinició el medidor"),
        required=False,
        help_text=_("Márcalo solo si el medidor volvió a cero o se reemplazó."),
    )
    # Generado al mostrar el formulario: un doble envío no registra dos lecturas.
    client_uuid = forms.UUIDField(widget=forms.HiddenInput, required=False, initial=uuid.uuid4)
