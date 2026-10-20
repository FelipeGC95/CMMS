"""Cifrado del secreto de cliente OIDC (ADR-032).

Fernet (AES-128-CBC + HMAC-SHA256) con una clave derivada de `SSO_SECRET_KEY`, o de
`SECRET_KEY` si no está definida. Un respaldo o una exportación de la base no traen el
secreto en claro. Cambiar la clave invalida los secretos guardados: el administrador los
vuelve a escribir (el inicio de sesión responde con un error claro mientras tanto).
"""

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings


class SecretUnavailableError(Exception):
    """El secreto guardado no se puede descifrar (la clave cambió)."""


def _fernet() -> Fernet:
    material = (getattr(settings, "SSO_SECRET_KEY", "") or settings.SECRET_KEY).encode()
    key = hashlib.sha256(b"galbust-sso-client-secret:" + material).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def encrypt(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode()


def decrypt(value: str) -> str:
    try:
        return _fernet().decrypt(value.encode()).decode()
    except (InvalidToken, ValueError) as error:
        raise SecretUnavailableError from error
