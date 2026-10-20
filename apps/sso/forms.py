from allauth.account.forms import ResetPasswordForm
from django import forms
from django.contrib import messages
from django.utils.translation import gettext
from django.utils.translation import gettext_lazy as _

from apps.accounts.models import CUSTOM_ROLE_BASES, CustomRole
from apps.accounts.permissions import CUSTOM_ROLES_MODULE
from apps.organizations.modules import has_module

from . import selectors

CUSTOM_PREFIX = "custom:"


class SsoConfigurationForm(forms.Form):
    discovery_url = forms.URLField(
        label=_("URL del emisor (issuer)"),
        max_length=500,
        assume_scheme="https",
        help_text=_(
            "La del inquilino de tu proveedor, p. ej. https://login.microsoftonline.com/<id>/v2.0. "
            "También sirve la URL completa de descubrimiento."
        ),
    )
    client_id = forms.CharField(label=_("ID de cliente (client ID)"), max_length=255)
    client_secret = forms.CharField(
        label=_("Secreto del cliente"),
        required=False,
        max_length=500,
        widget=forms.PasswordInput(render_value=False, attrs={"autocomplete": "new-password"}),
    )
    default_role = forms.ChoiceField(
        label=_("Rol de quien entra por primera vez"),
        help_text=_("Nunca administrador. Puedes cambiar el rol de cada persona después, en Equipo."),
    )
    is_active = forms.BooleanField(label=_("SSO activo"), required=False)
    enforce_sso = forms.BooleanField(
        label=_("Exigir SSO a los correos de los dominios verificados"),
        required=False,
        help_text=_(
            "No podrán entrar ni recuperar la contraseña: irán al SSO. "
            "Los administradores conservan contraseña y verificación en dos pasos como acceso de emergencia."
        ),
    )

    def __init__(self, *args, organization, has_secret=False, **kwargs):
        super().__init__(*args, **kwargs)
        choices = list(CUSTOM_ROLE_BASES)
        if has_module(organization, CUSTOM_ROLES_MODULE):
            label = gettext("Rol personalizado: %(name)s")
            choices += [
                (f"{CUSTOM_PREFIX}{role.pk}", label % {"name": role.name})
                for role in CustomRole.objects.filter(is_active=True).order_by("name")
            ]
        self.fields["default_role"].choices = choices
        secret = self.fields["client_secret"]
        if has_secret:
            secret.help_text = _("Guardado. Déjalo en blanco para conservarlo; no se vuelve a mostrar.")
        else:
            secret.required = True
            secret.help_text = _("Se guarda cifrado y no se vuelve a mostrar.")

    def service_data(self) -> dict:
        data = dict(self.cleaned_data)
        value = data.pop("default_role")
        if value.startswith(CUSTOM_PREFIX):
            data["default_role"] = ""
            data["default_custom_role"] = value.removeprefix(CUSTOM_PREFIX)
        else:
            data["default_role"] = value
            data["default_custom_role"] = None
        return data


def initial_role(configuration) -> str:
    if configuration.default_custom_role_id:
        return f"{CUSTOM_PREFIX}{configuration.default_custom_role_id}"
    return configuration.default_role


class SsoDomainForm(forms.Form):
    domain = forms.CharField(
        label=_("Dominio de correo"), max_length=253, help_text=_("Sin @, p. ej. empresa.cl.")
    )


class SsoLoginForm(forms.Form):
    email = forms.EmailField(
        label=_("Correo de tu empresa"),
        widget=forms.EmailInput(attrs={"autocomplete": "email", "autofocus": True}),
    )


class SsoAwareResetPasswordForm(ResetPasswordForm):
    """Recuperar contraseña (allauth) respetando «exigir SSO» (ORG-11 CA7).

    En un dominio que exige SSO solo los administradores reciben el enlace (acceso de
    emergencia); el resto ve que debe entrar por SSO. El aviso es el mismo para todos los
    correos del dominio, así no revela quién es administrador ni qué cuentas existen.
    """

    def save(self, request, **kwargs):
        email = self.cleaned_data["email"]
        if not selectors.enforced_domain(email):
            return super().save(request, **kwargs)
        messages.info(
            request,
            gettext(
                "Tu organización entra con inicio de sesión único (SSO): usa «Entrar con SSO». "
                "Si eres administrador, te enviamos el enlace para restablecer la contraseña."
            ),
        )
        self.users = [user for user in self.users if selectors.password_login_blocked(user) is None]
        if self.users:
            return super().save(request, **kwargs)
        return email
