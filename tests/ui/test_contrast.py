"""Contraste WCAG AA de los tokens de diseño (docs/06_sistema_de_diseno.md §6).

Toda combinación nueva de texto/fondo o control/fondo se agrega a CONTRAST_PAIRS.
"""

import re

import pytest

from apps.ui.tokens import TOKENS_PATH as TOKENS
from apps.ui.tokens import color_tokens

TEXT, UI = 4.5, 3.0

# (texto o control, fondo, mínimo)
CONTRAST_PAIRS = [
    ("--color-text", "--color-surface", TEXT),
    ("--color-text", "--color-bg", TEXT),
    ("--color-text-muted", "--color-surface", TEXT),
    ("--color-text-muted", "--color-bg", TEXT),
    ("--color-text-brand", "--color-surface", TEXT),
    ("--color-link", "--color-surface", TEXT),
    ("--color-link", "--color-bg", TEXT),
    ("--color-nav-text", "--color-nav-bg", TEXT),
    ("--color-nav-active", "--color-nav-bg", UI),
    ("--color-action-text", "--color-action-bg", TEXT),
    ("--color-action-text", "--color-action-bg-hover", TEXT),
    ("--color-secondary-text", "--color-secondary-bg", TEXT),
    ("--color-danger-text", "--color-danger-bg", TEXT),
    ("--color-border-strong", "--color-surface", UI),
    ("--color-focus", "--color-surface", UI),
    ("--color-focus", "--color-bg", UI),
    ("--color-success", "--color-success-bg", TEXT),
    ("--color-warning", "--color-warning-bg", TEXT),
    ("--color-error", "--color-error-bg", TEXT),
    ("--color-info", "--color-info-bg", TEXT),
    ("--color-neutral", "--color-neutral-bg", TEXT),
    ("--color-success", "--color-surface", TEXT),
    ("--color-warning", "--color-surface", TEXT),
    ("--color-error", "--color-surface", TEXT),
    ("--color-text", "--color-surface-sunken", TEXT),
    ("--color-text-muted", "--color-surface-sunken", TEXT),
    # Firma al completar la OT (WO-03 CA3): trazo y borde del lienzo.
    ("--color-signature-ink", "--color-signature-bg", TEXT),
    ("--color-border-strong", "--color-signature-bg", UI),
]


def _parse():
    themes = {theme: dict(values) for theme, values in color_tokens().items()}
    # Un fondo transparente se ve sobre la superficie.
    for theme in themes.values():
        for key, value in theme.items():
            if value == "transparent":
                theme[key] = theme["--color-surface"]
    return themes


def _luminance(hex_color):
    channels = [int(hex_color[i : i + 2], 16) / 255 for i in (1, 3, 5)]
    lin = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def contrast(a, b):
    la, lb = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


THEMES = _parse()


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("fg,bg,minimum", CONTRAST_PAIRS, ids=lambda v: str(v))
def test_token_contrast(theme, fg, bg, minimum):
    tokens = THEMES[theme]
    ratio = contrast(tokens[fg], tokens[bg])
    assert ratio >= minimum, f"{theme}: {fg} {tokens[fg]} sobre {bg} {tokens[bg]} = {ratio:.2f}:1"


def test_brand_rules():
    light = THEMES["light"]
    assert THEMES["dark"]["--color-bg"] != light["--color-bg"], "no se encontró el bloque de modo oscuro"
    # Nunca texto blanco sobre cian (2,5:1) ni cian como texto sobre claro.
    assert contrast("#FFFFFF", light["--color-action-bg"]) < TEXT
    assert light["--color-link"].upper() != "#12B3C8"
    assert light["--color-action-text"].upper() == "#0B2E4F"


def test_no_hardcoded_colors_outside_tokens():
    root = TOKENS.parents[2]
    offenders = []
    for path in [root / "static" / "css" / "components.css", *(root / "templates").rglob("*.html")]:
        text = path.read_text(encoding="utf-8")
        if path.name == "base.html":
            text = text.replace('content="#0B2E4F"', "")  # meta theme-color, no es CSS
        if re.search(r"#[0-9A-Fa-f]{3,6}\b|rgb\(", text):
            offenders.append(path.name)
    assert offenders == []
