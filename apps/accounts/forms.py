import secrets
import time

from django import forms
from django.conf import settings
from django.contrib.auth.password_validation import validate_password
from django.core import signing
from django.utils.translation import gettext_lazy as _

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

    def __init__(self, *args, locations, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["locations"].queryset = locations

    def clean(self):
        data = super().clean()
        scoped = data.get("restricted_to_locations") or data.get("role") == Membership.Role.EXTERNAL
        if scoped and not data.get("locations"):
            self.add_error("locations", _("Elige al menos una ubicación para limitar el acceso."))
        return data


CHALLENGE_SALT = "accounts.signup.challenge"
CHALLENGE_MAX_AGE = 60 * 60  # el formulario sirve una hora


class SignupForm(forms.Form):
    """Autoregistro (ORG-02) con protección anti-bots sin JS de terceros (CA3, CSP estricta).

    Tres barreras livianas, todas en el servidor: un campo trampa oculto que una persona
    deja vacío, un tiempo mínimo entre mostrar y enviar el formulario, y una suma simple.
    La suma y la hora viajan firmadas en un campo oculto (no se guardan en la sesión).
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
    answer = forms.IntegerField(label="", widget=forms.NumberInput(attrs={"inputmode": "numeric"}))
    challenge = forms.CharField(widget=forms.HiddenInput)
    # Campo trampa: oculto con CSS y fuera del orden de tabulación.
    website = forms.CharField(
        label=_("Deja este campo vacío"),
        required=False,
        widget=forms.TextInput(attrs={"autocomplete": "off", "tabindex": "-1"}),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        payload = self._payload(self.data.get("challenge")) if self.is_bound else None
        # Un envío con el token ausente o adulterado se rechaza siempre; el desafío nuevo
        # solo sirve para volver a mostrar el formulario (si no, una suma igual por azar pasaría).
        self._challenge_invalid = self.is_bound and payload is None
        if payload is None:
            payload = {"a": secrets.randbelow(9) + 1, "b": secrets.randbelow(9) + 1, "t": time.time()}
            token = signing.dumps(payload, salt=CHALLENGE_SALT)
            if self.is_bound:
                self.data = self.data.copy()
                self.data["challenge"] = token
            self.initial["challenge"] = token
        self._challenge = payload
        self.fields["answer"].label = _("Para comprobar que eres una persona: ¿cuánto es %(a)s + %(b)s?") % {
            "a": payload["a"],
            "b": payload["b"],
        }

    @staticmethod
    def _payload(token):
        try:
            payload = signing.loads(token or "", salt=CHALLENGE_SALT)
        except signing.BadSignature:
            return None
        if not isinstance(payload, dict) or not {"a", "b", "t"} <= payload.keys():
            return None
        return payload

    def clean_email(self):
        return self.cleaned_data["email"].strip().lower()

    def clean(self):
        data = super().clean()
        not_human = forms.ValidationError(
            _("No pudimos comprobar que eres una persona. Revisa la suma e inténtalo de nuevo.")
        )
        elapsed = time.time() - self._challenge["t"]
        if data.get("website") or self._challenge_invalid:
            raise not_human
        if elapsed < settings.SIGNUP_MIN_SECONDS or elapsed > CHALLENGE_MAX_AGE:
            raise not_human
        if data.get("answer") is not None and data["answer"] != self._challenge["a"] + self._challenge["b"]:
            self.add_error("answer", _("La suma no es correcta."))
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
