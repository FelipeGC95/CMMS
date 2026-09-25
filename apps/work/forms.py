import uuid

from django import forms
from django.utils.translation import gettext_lazy as _

from apps.assets.models import Asset

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
    failure_mechanism = forms.ModelChoiceField(
        label=_("Mecanismo de falla"), queryset=FailureCode.all_tenants.none(), required=False
    )
    detection_method = forms.ModelChoiceField(
        label=_("Método de detección"), queryset=FailureCode.all_tenants.none(), required=False
    )
    component = forms.ModelChoiceField(
        label=_("Componente afectado"), queryset=Asset.all_tenants.none(), required=False
    )

    meter_value = forms.DecimalField(
        label=_("Lectura del medidor"), min_value=0, max_digits=14, decimal_places=3
    )

    def __init__(
        self, *args, with_failure=False, meter=None, meter_required=False, require_failure_details=False,
        asset=None, **kwargs,
    ):  # fmt: skip
        super().__init__(*args, **kwargs)
        if meter is None:
            del self.fields["meter_value"]
        else:
            field = self.fields["meter_value"]
            field.label = f"{meter.get_kind_display()} al terminar"
            field.required = meter_required
            field.help_text = (
                _("Obligatorio: el próximo preventivo se calcula desde esta lectura.")
                if meter_required
                else _("Opcional: actualiza la lectura del equipo.")
            )
            if meter.current_value is not None:
                field.widget.attrs["placeholder"] = f"{meter.current_value.normalize():f}"
        if with_failure:
            self.fields["failure_mode"].queryset = failure_codes(FailureCode.Kind.MODE)
            self.fields["failure_cause"].queryset = failure_codes(FailureCode.Kind.CAUSE)
            self.fields["failure_mechanism"].queryset = failure_codes(FailureCode.Kind.MECHANISM)
            self.fields["detection_method"].queryset = failure_codes(FailureCode.Kind.DETECTION_METHOD)
            if asset is not None:
                self.fields["component"].queryset = asset.children.filter(is_active=True)
            if require_failure_details:
                self.fields["failure_mode"].required = True
                self.fields["failure_cause"].required = True
        else:
            del self.fields["failure_mode"]
            del self.fields["failure_cause"]
            del self.fields["failure_mechanism"]
            del self.fields["detection_method"]
            del self.fields["component"]


class FailureCodeForm(forms.Form):
    kind = forms.ChoiceField(label=_("Tipo"), choices=FailureCode.Kind.choices)
    code = forms.CharField(label=_("Código"), max_length=10)
    name = forms.CharField(label=_("Nombre"), max_length=120)


class FailureCodeRenameForm(forms.Form):
    name = forms.CharField(label=_("Nombre"), max_length=120)


class LaborForm(forms.Form):
    minutes = forms.IntegerField(label=_("Minutos trabajados"), min_value=1, max_value=24 * 60)


class PartUsageForm(forms.Form):
    description = forms.CharField(label=_("Repuesto o material"), max_length=200)
    quantity = forms.DecimalField(label=_("Cantidad"), min_value=0, max_digits=12, decimal_places=3)
    unit = forms.CharField(label=_("Unidad"), max_length=20, required=False)
    unit_cost = forms.DecimalField(label=_("Costo unitario"), min_value=0, max_digits=14, decimal_places=2)
    # Generado al mostrar el formulario: un doble envío no duplica la línea.
    client_uuid = forms.UUIDField(widget=forms.HiddenInput, required=False, initial=uuid.uuid4)


class OtherCostForm(forms.Form):
    description = forms.CharField(label=_("Descripción"), max_length=200)
    supplier = forms.CharField(label=_("Proveedor"), max_length=150, required=False)
    amount = forms.DecimalField(label=_("Monto"), min_value=0, max_digits=14, decimal_places=2)
