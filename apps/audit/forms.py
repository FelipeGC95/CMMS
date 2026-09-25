from django import forms
from django.utils.translation import gettext_lazy as _

from .models import AuditEvent


class TargetTypeField(forms.ModelChoiceField):
    def label_from_instance(self, obj):
        model = obj.model_class()
        return str(model._meta.verbose_name).capitalize() if model else obj.model


class AuditFilterForm(forms.Form):
    date_from = forms.DateField(
        label=_("Desde"), required=False, widget=forms.DateInput(attrs={"type": "date", "class": "input"})
    )
    date_to = forms.DateField(
        label=_("Hasta"), required=False, widget=forms.DateInput(attrs={"type": "date", "class": "input"})
    )
    actor = forms.CharField(label=_("Persona"), required=False, max_length=254)
    action = forms.ChoiceField(
        label=_("Acción"), required=False, choices=[("", _("Todas"))] + AuditEvent.Action.choices
    )
    target_type = TargetTypeField(
        label=_("Tipo de objeto"), required=False, queryset=None, empty_label=_("Todos")
    )

    def __init__(self, *args, target_types, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["target_type"].queryset = target_types
