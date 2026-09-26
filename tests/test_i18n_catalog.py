"""X-07: el catálogo es-CL (`locale/es/LC_MESSAGES/django.po`) está al día.

Se extraen de nuevo las cadenas con `makemessages` en una copia temporal de
`apps/` y `templates/` y se comparan los `msgid` con el catálogo del repo.
Necesita GNU gettext: en local se omite si no está instalado; en CI
(`REQUIRE_GETTEXT=1`) su ausencia es un error.
"""

import os
import pathlib
import shutil

import pytest
from django.core.management import call_command
from django.test import override_settings

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
CATALOG = REPO_ROOT / "locale" / "es" / "LC_MESSAGES" / "django.po"
REGENERATE = "uv run python manage.py makemessages"


def _unquote(line: str) -> str:
    return line.strip()[1:-1].encode("latin-1", "backslashreplace").decode("unicode_escape")


def po_entries(text: str) -> set[tuple[str, str, str]]:
    """(msgctxt, msgid, msgid_plural) de cada entrada, sin la cabecera."""
    entries, current, key, previous = set(), {}, None, None

    def flush():
        if current.get("msgid"):
            entries.add((current.get("msgctxt", ""), current["msgid"], current.get("msgid_plural", "")))
        current.clear()

    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith('"'):
            if key:
                current[key] += _unquote(line)
            continue
        keyword, _, rest = line.partition(" ")
        # Una entrada empieza en msgctxt, o en msgid si no lo antecede un msgctxt.
        if keyword == "msgctxt" or (keyword == "msgid" and previous != "msgctxt"):
            flush()
        previous = keyword
        key = keyword if keyword in ("msgctxt", "msgid", "msgid_plural") else None
        if key:
            current[key] = _unquote(rest)
    flush()
    return entries


def test_catalog_exists_and_contains_known_strings():
    entries = po_entries(CATALOG.read_text(encoding="utf-8"))
    msgids = {msgid for _, msgid, _ in entries}
    assert {"Saltar al contenido", "Falla reportada", "%(n)s activo creado"} <= msgids
    assert len(msgids) > 500


def test_po_parser_handles_context_plural_and_multiline():
    text = (
        'msgid ""\nmsgstr ""\n"Language: es\\n"\n\n'
        'msgctxt "unidad"\nmsgid "min"\nmsgstr ""\n\n'
        'msgid ""\n"Texto largo "\n"en dos líneas"\nmsgstr ""\n\n'
        'msgid "%(n)s activo"\nmsgid_plural "%(n)s activos"\nmsgstr[0] ""\nmsgstr[1] ""\n'
    )
    assert po_entries(text) == {
        ("unidad", "min", ""),
        ("", "Texto largo en dos líneas", ""),
        ("", "%(n)s activo", "%(n)s activos"),
    }


def test_catalog_is_up_to_date(tmp_path, monkeypatch):
    if not (shutil.which("xgettext") and shutil.which("msgmerge")):
        if os.environ.get("REQUIRE_GETTEXT"):
            pytest.fail("GNU gettext no está instalado (xgettext/msgmerge)")
        pytest.skip("GNU gettext no está instalado")
    for folder in ("apps", "templates"):
        shutil.copytree(
            REPO_ROOT / folder, tmp_path / folder, ignore=shutil.ignore_patterns("__pycache__", "*.pyc")
        )
    (tmp_path / "locale").mkdir()
    monkeypatch.chdir(tmp_path)
    with override_settings(LOCALE_PATHS=[tmp_path / "locale"]):
        call_command("makemessages", verbosity=0)

    fresh = po_entries((tmp_path / "locale" / "es" / "LC_MESSAGES" / "django.po").read_text(encoding="utf-8"))
    committed = po_entries(CATALOG.read_text(encoding="utf-8"))
    missing = sorted(msgid for _, msgid, _ in fresh - committed)
    stale = sorted(msgid for _, msgid, _ in committed - fresh)
    assert not missing and not stale, (
        f"El catálogo no está al día; ejecuta `{REGENERATE}`.\nFaltan: {missing[:20]}\nSobran: {stale[:20]}"
    )
