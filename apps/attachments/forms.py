import uuid

from django import forms
from django.utils.translation import gettext_lazy as _


class ManualForm(forms.Form):
    file = forms.FileField(
        label=_("Manual (PDF)"),
        help_text=_("PDF de hasta 20 MB. Se lee su texto para buscarlo después."),
        widget=forms.ClearableFileInput(attrs={"accept": "application/pdf"}),
    )
    # Generado al mostrar el formulario: un doble envío no duplica el manual.
    client_uuid = forms.UUIDField(widget=forms.HiddenInput, required=False, initial=uuid.uuid4)


class ManualSearchForm(forms.Form):
    q = forms.CharField(label=_("Buscar en manuales"), max_length=200, required=False)
