import uuid

from django import forms
from django.utils.translation import gettext_lazy as _

from .models import FailureCode
from .selectors import failure_codes


class FailureReportForm(forms.Form):
    description = forms.CharField(
        label=_("¿Qué pasó?"),
        widget=forms.Textarea(attrs={"rows": 4, "placeholder": _("Ej: pierde aceite por el costado")}),
        max_length=2000,
    )
    asset_stopped = forms.BooleanField(label=_("El equipo está detenido"), required=False)
    photo = forms.FileField(
        label=_("Foto (opcional)"),
        required=False,
        widget=forms.ClearableFileInput(attrs={"accept": "image/*", "capture": "environment"}),
    )
    # Generado al mostrar el formulario: un doble envío no crea dos avisos.
    client_uuid = forms.UUIDField(widget=forms.HiddenInput, required=False, initial=uuid.uuid4)


class CompleteWorkOrderForm(forms.Form):
    labor_minutes = forms.IntegerField(label=_("Tiempo trabajado (minutos)"), min_value=0, max_value=24 * 60)
    notes = forms.CharField(
        label=_("Comentario de cierre"), required=False, widget=forms.Textarea(attrs={"rows": 3})
    )
    failure_mode = forms.ModelChoiceField(
        label=_("Modo de falla"), queryset=FailureCode.all_tenants.none(), required=False
    )
    failure_cause = forms.ModelChoiceField(
        label=_("Causa"), queryset=FailureCode.all_tenants.none(), required=False
    )

    def __init__(self, *args, with_failure=False, **kwargs):
        super().__init__(*args, **kwargs)
        if with_failure:
            self.fields["failure_mode"].queryset = failure_codes(FailureCode.Kind.MODE)
            self.fields["failure_cause"].queryset = failure_codes(FailureCode.Kind.CAUSE)
        else:
            del self.fields["failure_mode"]
            del self.fields["failure_cause"]
