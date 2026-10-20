"""Alcances de los tokens de API por recurso (X-05, ADR-034).

Cada grupo de endpoints de la API v1 tiene `<grupo>:read` y `<grupo>:write`; `*:read` y
`*:write` cubren todos los grupos, también los que se agreguen después. Escribir implica
leer el mismo grupo. Los tokens anteriores ("solo lectura" / "lectura y escritura") se
migraron a `["*:read"]` y `["*:read", "*:write"]`: siguen funcionando igual.
"""

from dataclasses import dataclass

from django.utils.translation import gettext
from django.utils.translation import gettext_lazy as _
from rest_framework import permissions

READ, WRITE = "read", "write"
ALL = "*"
ALL_READ, ALL_WRITE = f"{ALL}:{READ}", f"{ALL}:{WRITE}"


@dataclass(frozen=True)
class ScopeGroup:
    key: str
    label: str
    module: str  # módulo del plan que habilita el grupo
    basenames: frozenset[str]  # basenames del router de la API v1


SCOPE_GROUPS: tuple[ScopeGroup, ...] = (
    ScopeGroup("assets", _("Activos"), "assets", frozenset({"api-asset"})),
    ScopeGroup("meters", _("Medidores y lecturas"), "assets", frozenset({"api-meter", "api-reading"})),
    ScopeGroup("work_orders", _("Órdenes de trabajo"), "work_orders", frozenset({"api-work-order"})),
    ScopeGroup("requests", _("Solicitudes"), "requests", frozenset({"api-request"})),
    ScopeGroup(
        "inventory",
        _("Inventario"),
        "inventory",
        frozenset({"api-part", "api-warehouse", "api-stock", "api-stock-movement"}),
    ),
    ScopeGroup(
        "condition", _("Condición"), "condition", frozenset({"api-condition-rule", "api-condition-alert"})
    ),
)
GROUP_BY_BASENAME = {basename: group.key for group in SCOPE_GROUPS for basename in group.basenames}
ALL_SCOPES = frozenset(
    {ALL_READ, ALL_WRITE} | {f"{group.key}:{mode}" for group in SCOPE_GROUPS for mode in (READ, WRITE)}
)

# Atajos del alcance general de antes (y de `create_token(scope=...)`).
LEGACY_SCOPES = {READ: [ALL_READ], WRITE: [ALL_READ, ALL_WRITE]}


def clean_scopes(scopes) -> list[str]:
    """Alcances conocidos, sin repetir y en orden estable (los generales primero)."""
    wanted = set(scopes or ())
    ordered = [ALL_READ, ALL_WRITE] + [f"{g.key}:{m}" for g in SCOPE_GROUPS for m in (READ, WRITE)]
    return [scope for scope in ordered if scope in wanted]


def allows(scopes, *, group: str, write: bool) -> bool:
    """¿Los alcances permiten leer (o escribir) el grupo? Escribir implica leer."""
    granted = set(scopes or ())
    if write:
        return bool(granted & {ALL_WRITE, f"{group}:{WRITE}"})
    return bool(granted & {ALL_READ, ALL_WRITE, f"{group}:{READ}", f"{group}:{WRITE}"})


def required_scope(*, basename: str | None, method: str) -> str | None:
    """Alcance que pide un endpoint de la API v1 (None si el endpoint no está en un grupo)."""
    group = GROUP_BY_BASENAME.get(basename or "")
    if group is None:
        return None
    return f"{group}:{READ if method.upper() in permissions.SAFE_METHODS else WRITE}"


def scope_labels(scopes) -> list[str]:
    """Etiquetas legibles para la lista de tokens: «Todo (lectura)», «Inventario (escritura)»…"""
    granted = set(scopes or ())
    labels = []
    groups = [(ALL, gettext("Todo"))] + [(g.key, g.label) for g in SCOPE_GROUPS]
    for key, label in groups:
        if f"{key}:{WRITE}" in granted:
            labels.append(gettext("%(group)s (lectura y escritura)") % {"group": label})
        elif f"{key}:{READ}" in granted:
            labels.append(gettext("%(group)s (lectura)") % {"group": label})
    return labels
