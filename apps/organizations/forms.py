from django import forms
from django.utils.translation import gettext_lazy as _

from .models import Location


class LocationForm(forms.Form):
    code = forms.CharField(label=_("Código"), max_length=30)
    name = forms.CharField(label=_("Nombre"), max_length=150)
    kind = forms.ChoiceField(label=_("Tipo"), choices=Location.Kind.choices, initial=Location.Kind.SITE)
    parent = forms.ModelChoiceField(
        label=_("Dentro de"), queryset=None, required=False, empty_label=_("— (ubicación principal)")
    )
    timezone = forms.CharField(
        label=_("Zona horaria"),
        max_length=64,
        required=False,
        help_text=_("En blanco usa la de la organización. Ej.: America/Santiago."),
    )
    is_active = forms.BooleanField(label=_("Activa"), required=False, initial=True)

    def __init__(self, *args, parents, parent_required=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["parent"].queryset = parents
        # Con alcance por sitio no se crean ubicaciones principales.
        self.fields["parent"].required = parent_required
