from django import forms
from django.utils.translation import gettext_lazy as _

from .models import Location
from .services import STATUS_LABEL_MAX_LENGTH


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


class OrganizationSettingsForm(forms.Form):
    require_close_approval = forms.BooleanField(
        label=_("Requiere aprobación para cerrar OT"),
        required=False,
        help_text=_(
            "Si está apagado, una OT completada pasa a cerrada automáticamente. "
            "Si está encendido, un supervisor o planificador la revisa y la cierra."
        ),
    )
    require_failure_details = forms.BooleanField(
        label=_("Exigir modo y causa de falla"),
        required=False,
        help_text=_("Al completar una OT correctiva, el modo y la causa de la falla son obligatorios."),
    )


class WorkOrderStatusLabelsForm(forms.Form):
    """Un campo por estado de OT; en blanco usa el nombre por defecto (WO-02 CA5)."""

    def __init__(self, *args, choices, **kwargs):
        super().__init__(*args, **kwargs)
        for key, default in choices:
            self.fields[key] = forms.CharField(
                label=default,
                max_length=STATUS_LABEL_MAX_LENGTH,
                required=False,
                widget=forms.TextInput(attrs={"placeholder": default}),
            )


class OrganizationLogoForm(forms.Form):
    """Logo para las etiquetas QR (ORG-09, parte mínima). Se valida y ajusta en el servicio."""

    logo = forms.FileField(
        label=_("Archivo del logo"),
        required=False,
        help_text=_("PNG, JPEG o WebP, hasta 1 MB. Se ajusta a 600 px; un fondo transparente se mantiene."),
        widget=forms.FileInput(attrs={"accept": "image/png,image/jpeg,image/webp"}),
    )
    remove = forms.BooleanField(label=_("Quitar el logo actual"), required=False)

    def clean(self):
        data = super().clean()
        if not data.get("logo") and not data.get("remove"):
            raise forms.ValidationError(_("Elige un archivo de imagen para el logo."))
        return data
