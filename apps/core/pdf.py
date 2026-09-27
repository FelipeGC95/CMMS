"""Fuentes compartidas de los PDF generados con reportlab (ADR-019).

Fuentes TTF incrustadas (DejaVu, licencia libre en apps/assets/fonts/LICENSE_DEJAVU.txt):
las estándar del PDF solo cubren Latin-1 y cortan nombres como «Ñandú – Nº 3 ø25».
"""

import io
from functools import cache
from pathlib import Path

from reportlab.lib.utils import ImageReader
from reportlab.pdfbase.pdfmetrics import registerFont
from reportlab.pdfbase.ttfonts import TTFont

FONTS_DIR = Path(__file__).resolve().parent.parent / "assets" / "fonts"
TEXT_FONT = "GalbustLabelSans"
BOLD_FONT = "GalbustLabelSans-Bold"
MONO_BOLD_FONT = "GalbustLabelMono-Bold"
FONT_FILES = {
    TEXT_FONT: "DejaVuSans.ttf",
    BOLD_FONT: "DejaVuSans-Bold.ttf",
    MONO_BOLD_FONT: "DejaVuSansMono-Bold.ttf",
}


@cache
def register_fonts() -> None:
    """Registra las fuentes una vez por proceso; reportlab incrusta solo los glifos usados."""
    for name, filename in FONT_FILES.items():
        registerFont(TTFont(name, str(FONTS_DIR / filename)))


def read_logo(organization):
    """Logo de la organización listo para dibujar, o None (sin logo, perdido o ilegible)."""
    if not organization.logo:
        return None
    try:
        with organization.logo.open("rb") as stored:
            return ImageReader(io.BytesIO(stored.read()))
    except (OSError, ValueError):
        return None
