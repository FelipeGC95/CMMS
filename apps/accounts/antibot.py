"""Protección anti-bots compartida para formularios públicos (ORG-02 CA3, REQ-02 CA2).

Sin JS de terceros (CSP estricta): tres barreras livianas, todas en el servidor.

- Un campo trampa oculto (`website`) que una persona deja vacío.
- Un tiempo mínimo entre mostrar y enviar el formulario.
- Una suma simple. La suma y la hora viajan firmadas en un campo oculto
  (`challenge`); no se guardan en la sesión.

Además, `consume_rate` cuenta intentos en el caché compartido (ventanas fijas)
para limitar envíos por IP, por activo u otra clave.
"""

import secrets
import time

from django import forms
from django.conf import settings
from django.core import signing
from django.core.cache import cache
from django.utils.translation import gettext_lazy as _

CHALLENGE_MAX_AGE = 60 * 60  # el formulario sirve una hora


class HumanCheckMixin:
    """Agrega `answer`, `challenge` y `website` al final del formulario y los valida en `clean`.

    Cada formulario usa su propia sal: un token de un formulario no sirve en otro.
    `min_seconds_setting` nombra el ajuste con los segundos mínimos (si falta, se usa
    `SIGNUP_MIN_SECONDS`).
    """

    challenge_salt = "accounts.signup.challenge"
    min_seconds_setting = "SIGNUP_MIN_SECONDS"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["answer"] = forms.IntegerField(
            label="", widget=forms.NumberInput(attrs={"inputmode": "numeric"})
        )
        self.fields["challenge"] = forms.CharField(widget=forms.HiddenInput)
        # Campo trampa: oculto con CSS y fuera del orden de tabulación.
        self.fields["website"] = forms.CharField(
            label=_("Deja este campo vacío"),
            required=False,
            widget=forms.TextInput(attrs={"autocomplete": "off", "tabindex": "-1"}),
        )
        payload = self._payload(self.data.get("challenge")) if self.is_bound else None
        # Un envío con el token ausente o adulterado se rechaza siempre; el desafío nuevo
        # solo sirve para volver a mostrar el formulario (si no, una suma igual por azar pasaría).
        self._challenge_invalid = self.is_bound and payload is None
        if payload is None:
            payload = {"a": secrets.randbelow(9) + 1, "b": secrets.randbelow(9) + 1, "t": time.time()}
            token = signing.dumps(payload, salt=self.challenge_salt)
            if self.is_bound:
                self.data = self.data.copy()
                self.data["challenge"] = token
            self.initial["challenge"] = token
        self._challenge = payload
        self.fields["answer"].label = _("Para comprobar que eres una persona: ¿cuánto es %(a)s + %(b)s?") % {
            "a": payload["a"],
            "b": payload["b"],
        }

    def _payload(self, token):
        try:
            payload = signing.loads(token or "", salt=self.challenge_salt)
        except signing.BadSignature:
            return None
        if not isinstance(payload, dict) or not {"a", "b", "t"} <= payload.keys():
            return None
        return payload

    def _min_seconds(self) -> float:
        return getattr(settings, self.min_seconds_setting, settings.SIGNUP_MIN_SECONDS)

    def clean(self):
        data = super().clean()
        not_human = forms.ValidationError(
            _("No pudimos comprobar que eres una persona. Revisa la suma e inténtalo de nuevo.")
        )
        elapsed = time.time() - self._challenge["t"]
        if data.get("website") or self._challenge_invalid:
            raise not_human
        if elapsed < self._min_seconds() or elapsed > CHALLENGE_MAX_AGE:
            raise not_human
        if data.get("answer") is not None and data["answer"] != self._challenge["a"] + self._challenge["b"]:
            self.add_error("answer", _("La suma no es correcta."))
        return data


def consume_rate(key: str, *, limit: int, period: int) -> bool:
    """Cuenta un intento para `key`; False si ya se superó `limit` en la ventana de `period` segundos.

    Ventana fija sobre el caché compartido (en base de datos: sirve entre workers).
    No es exacta bajo concurrencia alta; basta para frenar abusos.
    """
    cache_key = f"antibot:{key}:{int(time.time() // period)}"
    if cache.add(cache_key, 1, timeout=period):
        return limit >= 1
    try:
        count = cache.incr(cache_key)
    except ValueError:  # expiró entre `add` e `incr`
        cache.set(cache_key, 1, timeout=period)
        count = 1
    return count <= limit
