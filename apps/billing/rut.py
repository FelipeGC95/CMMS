"""RUT chileno (ORG-13 CA7): validación con dígito verificador (módulo 11) y formato 12.345.678-5."""

import re

from django.core.exceptions import ValidationError
from django.utils.translation import gettext as _

RUT_PATTERN = re.compile(r"^(\d{1,8})([\dK])$")


def check_digit(body: str) -> str:
    total, factor = 0, 2
    for digit in reversed(body):
        total += int(digit) * factor
        factor = 2 if factor == 7 else factor + 1
    remainder = 11 - total % 11
    return {11: "0", 10: "K"}.get(remainder, str(remainder))


def normalize_rut(value: str) -> str:
    """Valida y devuelve el RUT con puntos y guion; lanza ValidationError si no es válido."""
    cleaned = re.sub(r"[.\s-]", "", value or "").upper()
    match = RUT_PATTERN.match(cleaned)
    if not match:
        raise ValidationError(_("Escribe un RUT válido, p. ej. 76.123.456-0."))
    body, digit = match.groups()
    body = body.lstrip("0") or "0"
    if check_digit(body) != digit:
        raise ValidationError(_("El dígito verificador del RUT no corresponde."))
    return f"{int(body):,}".replace(",", ".") + f"-{digit}"
