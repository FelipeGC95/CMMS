from django import forms
from django.contrib.auth.password_validation import validate_password
from django.utils.translation import gettext
from django.utils.translation import gettext_lazy as _

from .antibot import HumanCheckMixin
from .models import CUSTOM_ROLE_BASES, Membership, User
from .permissions import PERM_GROUPS, PERM_LABELS, ROLE_PERMISSIONS

CUSTOM_PREFIX = "custom:"


class RoleChoiceMixin:
    """Selector de rol: roles base + personalizados activos (ORG-12).

    Un rol personalizado viaja como `custom:<id>`; `cleaned_data` deja `role` (rol base) y
    `custom_role` (o None). El servicio vuelve a validar el rol personalizado.
    """

    def _set_role_choices(self, custom_roles):
        self.custom_roles = {str(role.pk): role for role in custom_roles}
        if self.custom_roles:
            self.fields["role"].choices = [
                (_("Roles base"), list(Membership.Role.choices)),
                (
                    _("Roles personalizados"),
                    [(f"{CUSTOM_PREFIX}{pk}", role.name) for pk, role in self.custom_roles.items()],
                ),
            ]

    def clean_role(self):
        value = self.cleaned_data["role"]
        self.cleaned_data["custom_role"] = None
        if value.startswith(CUSTOM_PREFIX):
            role = self.custom_roles[value.removeprefix(CUSTOM_PREFIX)]
            self.cleaned_data["custom_role"] = role
            return role.base_role
        return value


class InviteForm(RoleChoiceMixin, forms.Form):
    email = forms.EmailField(label=_("Correo"))
    role = forms.ChoiceField(
        label=_("Rol"), choices=Membership.Role.choices, initial=Membership.Role.TECHNICIAN
    )

    def __init__(self, *args, custom_roles=(), **kwargs):
        super().__init__(*args, **kwargs)
        self._set_role_choices(custom_roles)


class MemberForm(RoleChoiceMixin, forms.Form):
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

    def __init__(self, *args, locations, custom_roles=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["locations"].queryset = locations
        self._set_role_choices(custom_roles)

    def clean(self):
        data = super().clean()
        scoped = data.get("restricted_to_locations") or data.get("role") == Membership.Role.EXTERNAL
        if scoped and not data.get("locations"):
            self.add_error("locations", _("Elige al menos una ubicación para limitar el acceso."))
        return data


class CustomRoleCreateForm(forms.Form):
    """Alta de rol personalizado (ORG-12): parte con los permisos del rol base."""

    name = forms.CharField(label=_("Nombre"), max_length=60, help_text=_("Por ejemplo, «Bodeguero»."))
    base_role = forms.ChoiceField(
        label=_("Rol base"),
        choices=CUSTOM_ROLE_BASES,
        initial=Membership.Role.TECHNICIAN,
        help_text=_(
            "Parte con sus permisos y define lo demás: «Mis OT» y capacidad del técnico, "
            "alcance obligatorio del externo."
        ),
    )


class CustomRoleForm(forms.Form):
    """Nombre y permisos del rol, con una casilla por permiso agrupada por área."""

    name = forms.CharField(label=_("Nombre"), max_length=60)

    def __init__(self, *args, role, **kwargs):
        super().__init__(*args, **kwargs)
        self.role = role
        base = ROLE_PERMISSIONS[role.base_role]
        for key, label, perms in PERM_GROUPS:
            self.fields[f"perms_{key}"] = forms.MultipleChoiceField(
                label=label,
                required=False,
                widget=forms.CheckboxSelectMultiple,
                choices=[(perm.value, self._label(perm, perm in base)) for perm in perms],
            )
            self.initial.setdefault(
                f"perms_{key}", [perm.value for perm in perms if perm.value in role.permissions]
            )
        self.initial.setdefault("name", role.name)

    @staticmethod
    def _label(perm, in_base):
        if in_base:
            return gettext("%(label)s (del rol base)") % {"label": PERM_LABELS[perm]}
        return str(PERM_LABELS[perm])

    def permissions(self) -> list[str]:
        return [code for key, _label, _perms in PERM_GROUPS for code in self.cleaned_data[f"perms_{key}"]]


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
