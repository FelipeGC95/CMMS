"""Deuda de traducción: ningún `messages.*` de una vista pasa un literal sin
marcar (CLAUDE.md: "Nunca texto visible sin marcar").

Un AST simple por archivo: para cada llamada `messages.success/error/warning/
info(request, ...)`, el segundo argumento no puede ser un string ni un
f-string crudos. Debe venir de `_(...)`/`gettext(...)` (con o sin `%` de
formato), de una variable (típicamente un mensaje ya traducido en el origen,
como `exc.message` o `str(error)`) o de una llamada como `" ".join(...)`.
"""

import ast
import pathlib

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
VIEW_FILES = sorted((REPO_ROOT / "apps").glob("*/*views.py"))
MESSAGE_METHODS = {"success", "error", "warning", "info", "debug"}


def _is_unmarked_literal(node: ast.AST) -> bool:
    """Un string o f-string escrito directamente en la llamada, sin pasar por gettext."""
    if isinstance(node, ast.JoinedStr):  # f"..."
        return True
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):  # "Texto: " + algo
        return _is_unmarked_literal(node.left) or _is_unmarked_literal(node.right)
    return isinstance(node, ast.Constant) and isinstance(node.value, str)


def _violations(path: pathlib.Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        is_messages_call = (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id == "messages"
            and func.attr in MESSAGE_METHODS
        )
        if not is_messages_call or len(node.args) < 2:
            continue
        message_arg = node.args[1]
        if _is_unmarked_literal(message_arg):
            found.append(
                f"{path.relative_to(REPO_ROOT)}:{node.lineno}: messages.{func.attr}(...) sin gettext"
            )
    return found


@pytest.mark.parametrize("path", VIEW_FILES, ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_view_messages_are_marked_for_translation(path):
    violations = _violations(path)
    assert not violations, "\n".join(violations)


def test_there_are_views_to_check():
    """Si esto falla, el glob de arriba dejó de encontrar apps/*/views.py."""
    assert len(VIEW_FILES) >= 5
