"""Lectura de los design tokens de static/css/tokens.css.

Lo usan la página /design-system/ y el test de contraste, para que ambos
trabajen siempre sobre los valores reales del archivo.
"""

import re
from functools import lru_cache
from pathlib import Path

from django.conf import settings

TOKENS_PATH = Path(settings.BASE_DIR) / "static" / "css" / "tokens.css"
DARK_SELECTOR = ':root[data-theme="dark"]'
COLOR_DECL = re.compile(r"(--color-[\w-]+)\s*:\s*(#[0-9A-Fa-f]{6}|transparent|rgb\([^)]*\))\s*;")


@lru_cache(maxsize=1)
def color_tokens() -> dict[str, dict[str, str]]:
    """{"light": {token: valor}, "dark": {...}}; el modo oscuro hereda lo que no redefine."""
    css = TOKENS_PATH.read_text(encoding="utf-8")
    light_block, _, dark_block = css.partition(DARK_SELECTOR)
    light = dict(COLOR_DECL.findall(light_block))
    dark = {**light, **dict(COLOR_DECL.findall(dark_block))}
    return {"light": light, "dark": dark}
