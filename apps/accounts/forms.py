from django import forms
from django.contrib.auth.password_validation import validate_password
from django.utils.translation import gettext_lazy as _

from .antibot import HumanCheckMixin
from .models import Membership, User


class InviteForm(forms.Form):
    email = forms.EmailField(label=_("Correo"))
    role = forms.ChoiceField(
        label=_("Rol"), choices=Membership.Role.choices, initial=Membership.Role.TECHNICIAN
    )


class MemberForm(forms.Form):
    role = forms.ChoiceField(label=_("Rol"), choices=Membership.Role.choices)
    restricted_to_locations = forms.BooleanField(
        label=_("Limitar a ciertas ubicaciones"),
        required=False,
        help_text=_(
            "Solo verá activos, OT e indicadores de las ubicaciones marcadas y las que dependen de ellas."
        ),
    )
    locations = forms.ModelMultipleChoiceField(
        label=_("Ubicaciones"), queryset=None, required=False, widget=forms.CheckboxSelectMultiple
    )
    weekly_capacity_hours = forms.DecimalField(
        label=_("Horas de trabajo por semana"),
        min_value=0,
        max_value=168,
        max_digits=4,
        decimal_places=1,
        required=False,  # vacío: se mantiene la capacidad actual
        help_text=_("Capacidad para el backlog y la programación semanal; se reparte de lunes a viernes."),
    )

    def __init__(self, *args, locations, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["locations"].queryset = locations

    def clean(self):
        data = super().clean()
        scoped = data.get("restricted_to_locations") or data.get("role") == Membership.Role.EXTERNAL
        if scoped and not data.get("locations"):
            self.add_error("locations", _("Elige al menos una ubicación para limitar el acceso."))
        return data


class SignupForm(HumanCheckMixin, forms.Form):
    """Autoregistro (ORG-02) con protección anti-bots sin JS de terceros (CA3, CSP estricta).

    Campo trampa, tiempo mínimo y suma firmada: ver `apps.accounts.antibot`.
    """

    full_name = forms.CharField(label=_("Tu nombre"), max_length=150)
    email = forms.EmailField(label=_("Correo de trabajo"))
    organization_name = forms.CharField(
        label=_("Nombre de tu empresa"),
        max_length=150,
        help_text=_("Así se verá tu organización. Podrás cambiarlo después."),
    )
    password = forms.CharField(
        label=_("Contraseña"),
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
        help_text=_("Al menos 10 caracteres."),
    )

    def clean_email(self):
        return self.cleaned_data["email"].strip().lower()

    def clean(self):
        data = super().clean()  # anti-bots primero (HumanCheckMixin)
        password = data.get("password")
        if password:
            try:
                validate_password(
                    password, User(email=data.get("email", ""), full_name=data.get("full_name", ""))
                )
            except forms.ValidationError as error:
                self.add_error("password", error)
        return data


class AcceptInvitationForm(forms.Form):
    full_name = forms.CharField(label=_("Nombre completo"), max_length=150)
    password1 = forms.CharField(
        label=_("Contraseña"), widget=forms.PasswordInput(attrs={"autocomplete": "new-password"})
    )
    password2 = forms.CharField(
        label=_("Repite la contraseña"), widget=forms.PasswordInput(attrs={"autocomplete": "new-password"})
    )

    def __init__(self, *args, email, **kwargs):
        super().__init__(*args, **kwargs)
        self.email = email

    def clean(self):
        data = super().clean()
        password = data.get("password1")
        if password and password != data.get("password2"):
            self.add_error("password2", _("Las contraseñas no coinciden."))
        elif password:
            try:
                validate_password(password, User(email=self.email, full_name=data.get("full_name", "")))
            except forms.ValidationError as error:
                self.add_error("password1", error)
        return data
