from django import forms
from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

from .models import Period
from .rut import normalize_rut


class BillingDetailsForm(forms.Form):
    tax_id = forms.CharField(label=_("RUT"), max_length=14, required=False, help_text=_("Ej.: 76.123.456-0"))
    legal_name = forms.CharField(label=_("Razón social"), max_length=150, required=False)
    business_activity = forms.CharField(label=_("Giro"), max_length=150, required=False)
    billing_address = forms.CharField(label=_("Dirección"), max_length=250, required=False)
    billing_email = forms.EmailField(
        label=_("Correo de facturación"),
        required=False,
        help_text=_("Recibe los avisos de cobro. En blanco, el de quien contrata."),
    )

    def clean_tax_id(self):
        value = self.cleaned_data["tax_id"].strip()
        if not value:
            return ""
        try:
            return normalize_rut(value)
        except ValidationError as error:
            raise forms.ValidationError(error.messages) from error


class CheckoutForm(forms.Form):
    plan = forms.CharField(max_length=50)
    period = forms.ChoiceField(choices=Period.choices)


class ChangePlanForm(forms.Form):
    plan = forms.CharField(max_length=50)
