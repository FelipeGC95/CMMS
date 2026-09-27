"""Color primario por organización (ORG-09) con contraste AA garantizado.

El administrador elige un color. Se usa en el botón primario (fondo + texto), en
los enlaces y el foco sobre fondos claros y oscuros, y en el indicador de la
navegación activa. Para cada uso se calcula un tono que cumpla WCAG AA contra
los fondos reales de `static/css/tokens.css`; si el color elegido no alcanza, se
aclara u oscurece solo lo necesario y se informa (CA1). Solo se sobrescriben los
tokens de `BRANDABLE_TOKENS`: los de estado y accesibilidad nunca (CA2).
"""

import colorsys
import re
from dataclasses import dataclass, field

from django.utils.translation import gettext as _

from apps.ui.tokens import color_tokens

HEX = re.compile(r"^#[0-9a-fA-F]{6}$")
TEXT, UI = 4.5, 3.0
WHITE = "#FFFFFF"

# Únicos tokens que una organización puede cambiar. Estados (--color-success…), foco,
# texto, superficies y bordes quedan siempre con los valores de GALBUST.
BRANDABLE_TOKENS = (
    "--color-action-bg",
    "--color-action-text",
    "--color-action-bg-hover",
    "--color-link",
    "--color-accent-text",
    "--color-nav-active",
)


def normalize(value: str) -> str:
    value = (value or "").strip()
    if not HEX.match(value):
        raise ValueError(value)
    return value.upper()


def _luminance(hex_color: str) -> float:
    channels = [int(hex_color[i : i + 2], 16) / 255 for i in (1, 3, 5)]
    lin = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def contrast(a: str, b: str) -> float:
    la, lb = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def _to_hls(hex_color):
    return colorsys.rgb_to_hls(*(int(hex_color[i : i + 2], 16) / 255 for i in (1, 3, 5)))


def _from_hls(h, lightness, s) -> str:
    r, g, b = colorsys.hls_to_rgb(h, min(1.0, max(0.0, lightness)), s)
    return f"#{round(r * 255):02X}{round(g * 255):02X}{round(b * 255):02X}"


def _fit(color: str, backgrounds: list[str], minimum: float, *, darker: bool) -> str:
    """El tono más cercano a `color` (misma matiz) que alcanza `minimum` contra todos los fondos."""
    h, lightness, s = _to_hls(color)
    step = -0.01 if darker else 0.01
    candidate = color
    while min(contrast(candidate, bg) for bg in backgrounds) < minimum:
        lightness += step
        if not 0.0 <= lightness <= 1.0:
            return "#000000" if darker else WHITE
        candidate = _from_hls(h, lightness, s)
    return candidate


@dataclass
class BrandPalette:
    color: str
    light: dict = field(default_factory=dict)  # tokens en :root
    dark: dict = field(default_factory=dict)  # tokens en :root[data-theme="dark"]
    notes: list = field(default_factory=list)  # ajustes, para informar al administrador


def brand_palette(color: str) -> BrandPalette:
    color = normalize(color)
    base = color_tokens()
    light, dark = base["light"], base["dark"]
    palette = BrandPalette(color=color)

    # Botón primario: el texto que más contrasta (azul GALBUST o blanco); si ninguno
    # llega a AA, se ajusta el fondo alejándolo del texto.
    brand_text = light["--color-action-text"]
    text = max((brand_text, WHITE), key=lambda t: contrast(color, t))
    action = color
    if contrast(action, text) < TEXT:
        action = _fit(color, [text], TEXT, darker=text == WHITE)
        palette.notes.append(
            _("El botón principal usa %(color)s para que su texto se lea bien.") % {"color": action}
        )
    h, lightness, s = _to_hls(action)
    hover = _from_hls(h, lightness + (0.08 if text != WHITE else -0.08), s)
    if contrast(hover, text) < TEXT:
        hover = action

    # Enlaces y foco: AA de texto sobre la superficie y el fondo de cada tema.
    link_light = _fit(color, [light["--color-surface"], light["--color-bg"]], TEXT, darker=True)
    link_dark = _fit(color, [dark["--color-surface"], dark["--color-bg"]], TEXT, darker=False)
    if link_light != color:
        palette.notes.append(
            _("Los enlaces sobre fondo claro usan %(color)s, un tono más oscuro.") % {"color": link_light}
        )
    if link_dark != color:
        palette.notes.append(
            _("En modo oscuro los enlaces usan %(color)s, un tono más claro.") % {"color": link_dark}
        )
    # Indicador de la navegación activa: contraste de componente sobre la barra.
    nav_light = _fit(color, [light["--color-nav-bg"]], UI, darker=False)
    nav_dark = _fit(color, [dark["--color-nav-bg"]], UI, darker=False)

    palette.light = {
        "--color-action-bg": action,
        "--color-action-text": text,
        "--color-action-bg-hover": hover,
        "--color-link": link_light,
        "--color-accent-text": link_light,
        "--color-nav-active": nav_light,
    }
    palette.dark = {
        "--color-link": link_dark,
        "--color-accent-text": link_dark,
        "--color-nav-active": nav_dark,
    }
    return palette


def theme_css(palette: BrandPalette) -> str:
    """Hoja de estilos con los tokens de la organización (se enlaza después de tokens.css)."""

    def block(selector, tokens):
        body = "".join(f"  {name}: {value};\n" for name, value in tokens.items() if name in BRANDABLE_TOKENS)
        return f"{selector} {{\n{body}}}\n"

    return (
        f"/* Color de la organización {palette.color} (ORG-09). */\n"
        + block(":root", palette.light)
        + block(':root[data-theme="dark"]', palette.dark)
    )
