"""X-07: ninguna plantilla muestra texto sin marcar para traducción.

Heurística: se quitan de la plantilla los comentarios, los bloques
`{% blocktranslate %}` y todas las etiquetas y variables de Django; lo que
queda se analiza como HTML. Cualquier palabra visible (texto de un nodo o
atributos `placeholder`, `title`, `aria-label`, `alt`) que no esté en la
lista permitida es texto sin marcar. `{% translate "…" %}` desaparece al
quitar las etiquetas, así que el texto marcado nunca cuenta.
"""

import pathlib
import re
from html.parser import HTMLParser

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
TEMPLATES = sorted((REPO_ROOT / "templates").rglob("*.html"))

# Marcas, siglas y unidades que se escriben igual en todos los idiomas.
ALLOWED_WORDS = {"GALBUST", "CMMS", "MTBF", "MTTR"}
# Referencia interna del sistema de diseño: su contenido son datos de ejemplo.
ALLOWED_FILES = {"ui/design_system.html"}

VISIBLE_ATTRS = {"placeholder", "title", "aria-label", "alt"}
WORD = re.compile(r"[A-Za-zÁÉÍÓÚÜÑáéíóúüñ]{2,}")


def _strip_django(source: str) -> str:
    source = re.sub(r"\{%\s*comment\s*%\}.*?\{%\s*endcomment\s*%\}", "", source, flags=re.S)
    source = re.sub(r"\{#.*?#\}", "", source, flags=re.S)
    source = re.sub(r"\{%\s*blocktrans(late)?\b.*?\{%\s*endblocktrans(late)?\s*%\}", "", source, flags=re.S)
    source = re.sub(r"\{%.*?%\}", "", source, flags=re.S)
    return re.sub(r"\{\{.*?\}\}", "", source, flags=re.S)


def _untranslated_words(text: str) -> list[str]:
    return [w for w in WORD.findall(text) if w not in ALLOWED_WORDS]


class _VisibleTextParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.raw_depth = 0  # dentro de <script> o <style>
        self.found: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.raw_depth += 1
        for name, value in attrs:
            if name in VISIBLE_ATTRS and value and _untranslated_words(value):
                self.found.append(f"línea {self.getpos()[0]}: {tag}[{name}]={value!r}")

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.raw_depth -= 1

    def handle_data(self, data):
        if not self.raw_depth and _untranslated_words(data):
            self.found.append(f"línea {self.getpos()[0]}: {data.strip()!r}")


def untranslated_text(source: str) -> list[str]:
    parser = _VisibleTextParser()
    parser.feed(_strip_django(source))
    parser.close()
    return parser.found


def _rel(path: pathlib.Path) -> str:
    return path.relative_to(REPO_ROOT / "templates").as_posix()


@pytest.mark.parametrize("path", TEMPLATES, ids=_rel)
def test_template_has_no_untranslated_text(path):
    if _rel(path) in ALLOWED_FILES:
        pytest.skip("datos de ejemplo del sistema de diseño")
    found = untranslated_text(path.read_text(encoding="utf-8"))
    assert not found, "Texto visible sin {% translate %}:\n" + "\n".join(found)


def test_detector_catches_bare_text_and_attributes():
    source = (
        '{% load i18n %}<p>{% translate "Hola" %}</p><p>Sin marcar</p>'
        '<input placeholder="Buscar"><img alt="{% translate \'Logo\' %}">'
        "{% blocktranslate %}Hola {{ name }}{% endblocktranslate %}"
        "<script>const x = 'texto';</script><span>GALBUST CMMS · {{ value }} MTTR</span>"
    )
    found = untranslated_text(source)
    assert len(found) == 2
    assert "Sin marcar" in found[0]
    assert "placeholder" in found[1]


def test_there_are_templates_to_check():
    assert len(TEMPLATES) >= 40
