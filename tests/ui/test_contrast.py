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
    # Calendario de preventivos (PM-06): el día de hoy resaltado en la grilla del mes.
    ("--color-text", "--color-info-bg", TEXT),
    ("--color-text-muted", "--color-info-bg", TEXT),
    ("--color-link", "--color-info-bg", TEXT),
    # Tablero semanal (REQ-05): contorno del día donde se suelta una OT (hoy, fondo info).
    ("--color-focus", "--color-info-bg", UI),
    # Vista de terreno (ADR-011): borde de las OT asignadas a la persona. El estado de conexión
    # de la barra (--color-nav-text y --color-nav-active sobre --color-nav-bg) y el título como
    # enlace (--color-link sobre --color-surface) ya están arriba.
    ("--color-accent-text", "--color-surface", UI),
    ("--color-accent-text", "--color-bg", UI),
    # Terreno, cola y sincronización (P4): cada fila de la barra y su botón usan el color del
    # estado sobre su fondo (pares de arriba); "guardado" y los detalles de un rechazo van en
    # --color-success / --color-error sobre la tarjeta (pares de arriba).
    ("--color-success", "--color-success-bg", TEXT),
    ("--color-warning", "--color-warning-bg", TEXT),
    ("--color-error", "--color-error-bg", TEXT),
    ("--color-info", "--color-info-bg", TEXT),
    # Gráficos de indicadores (KPI-02): barras y columnas sobre el riel (fondo hundido) y las
    # muestras de la leyenda sobre la tarjeta (--color-accent-text, --color-warning y
    # --color-border-strong sobre --color-surface ya están arriba).
    ("--color-accent-text", "--color-surface-sunken", UI),
    ("--color-warning", "--color-surface-sunken", UI),
    ("--color-border-strong", "--color-surface-sunken", UI),
    # Inventario (INV-12): fichas contador del inicio. Ícono del estado sobre la ficha; en la ficha
    # elegida el ícono toma el texto (blanco sobre azul); el número va sobre su pastilla hundida.
    # Las marcas de stock son badges (pares de arriba).
    ("--color-warning", "--color-surface", UI),
    ("--color-error", "--color-surface", UI),
    ("--color-info", "--color-surface", UI),
    ("--color-text", "--color-surface-sunken", TEXT),
    ("--color-secondary-text", "--color-secondary-bg", TEXT),
    # Tendencia de una variable (CBM-07): línea y puntos (--color-accent-text) y umbrales y lecturas
    # fuera de rango (--color-error) sobre el fondo hundido del gráfico; muestras de la leyenda sobre
    # la tarjeta (--color-accent-text y --color-error sobre --color-surface ya están arriba).
    ("--color-error", "--color-surface-sunken", UI),
    # Movimientos de inventario (INV-05, INV-07): el repuesto elegido va sobre la tarjeta con borde
    # fuerte (texto, texto atenuado, enlace "Cambiar" y borde sobre --color-surface, pares de arriba).
    # Asistente de diagnóstico (AI-01): quién habla, sobre la burbuja de la pregunta (fondo hundido).
    ("--color-text-brand", "--color-surface-sunken", TEXT),
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
