"""Uso completo desde un smartphone (ADR-017, docs/06_sistema_de_diseno.md §6).

Bajo 720 px cada fila de una `.table` se apila como tarjeta y `data-label`
reemplaza al encabezado. Toda celda lo declara; `data-label=""` marca una
celda que se explica sola (nombre, estado, acciones).
"""

import re
from pathlib import Path

import pytest

TEMPLATES = Path(__file__).resolve().parents[2] / "templates"

TABLE = re.compile(r'<table\b[^>]*class="[^"]*\btable\b[^"]*"[^>]*>(.*?)</table>', re.S)
TD = re.compile(r"<td\b[^>]*>")


def _templates():
    return sorted(TEMPLATES.rglob("*.html"))


@pytest.mark.parametrize("path", _templates(), ids=lambda p: str(p.relative_to(TEMPLATES)))
def test_table_cells_declare_data_label(path):
    source = path.read_text(encoding="utf-8")
    missing = [td for table in TABLE.findall(source) for td in TD.findall(table) if "data-label" not in td]
    assert not missing, f"Celdas sin data-label en {path.name}: {missing}"


@pytest.mark.parametrize("layout", ["base.html", "allauth/layouts/base.html"])
def test_layouts_declare_viewport(layout):
    source = (TEMPLATES / layout).read_text(encoding="utf-8")
    assert 'name="viewport" content="width=device-width, initial-scale=1"' in source
