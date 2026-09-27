"""ORG-09: el color de la organización se ajusta hasta cumplir AA en todos sus usos (CA1)
y nunca toca los tokens de estado ni de accesibilidad (CA2)."""

import pytest

from apps.organizations.branding import BRANDABLE_TOKENS, brand_palette, theme_css
from tests.ui.test_contrast import CONTRAST_PAIRS, THEMES, contrast

# Colores difíciles: muy claros, muy oscuros, saturados, grises, el cian de GALBUST.
SAMPLES = ["#12B3C8", "#FFFF00", "#FFFFFF", "#000000", "#FF0000", "#808080", "#0B2E4F", "#F4F7FA", "#7C3AED"]


def themed_tokens(color, theme):
    palette = brand_palette(color)
    tokens = dict(THEMES[theme])
    tokens.update(palette.light)
    if theme == "dark":
        tokens.update(palette.dark)
    return tokens


@pytest.mark.parametrize("color", SAMPLES)
@pytest.mark.parametrize("theme", ["light", "dark"])
def test_every_contrast_pair_still_passes_with_brand_color(color, theme):
    tokens = themed_tokens(color, theme)
    for fg, bg, minimum in CONTRAST_PAIRS:
        ratio = contrast(tokens[fg], tokens[bg])
        assert ratio >= minimum, f"{color} {theme}: {fg} {tokens[fg]} sobre {bg} {tokens[bg]} = {ratio:.2f}"


def test_readable_color_is_used_as_is():
    palette = brand_palette("#0E7C8C")  # ya cumple sobre claro
    assert palette.light["--color-link"] == "#0E7C8C"
    assert palette.light["--color-action-bg"] == "#0E7C8C"


def test_adjustments_are_reported():
    palette = brand_palette("#FFFF00")
    assert palette.light["--color-link"] != "#FFFF00"
    assert any("enlaces" in note for note in palette.notes)


def test_cyan_never_gets_white_text():
    assert brand_palette("#12B3C8").light["--color-action-text"] != "#FFFFFF"


def test_status_tokens_are_never_overridden():
    css = theme_css(brand_palette("#FF0000"))
    for token in (
        "--color-success",
        "--color-warning",
        "--color-error",
        "--color-info",
        "--color-text",
        "--color-focus",
    ):
        assert token not in css
    declared = {line.split(":")[0].strip() for line in css.splitlines() if line.strip().startswith("--")}
    assert declared <= set(BRANDABLE_TOKENS)


def test_invalid_color_is_rejected():
    with pytest.raises(ValueError):
        brand_palette("rojo")
