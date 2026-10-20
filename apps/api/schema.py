"""Esquema OpenAPI de la API v1 con el alcance que pide cada operación (ADR-034).

Módulo aparte de `framework`: drf-spectacular importa los ajustes de DRF, que a su vez
importan las clases de `framework` (importarlo allí sería circular).
"""

from django.utils.translation import gettext as _
from drf_spectacular.openapi import AutoSchema

from .scopes import required_scope


class ScopedAutoSchema(AutoSchema):
    """OpenAPI: cada operación de la API v1 declara su alcance (`x-required-scope`)."""

    def _required_scope(self):
        return required_scope(basename=getattr(self.view, "basename", None), method=self.method)

    def get_description(self):
        description = super().get_description()
        scope = self._required_scope()
        if scope is None:
            return description
        note = _("Alcance requerido: `%(scope)s`.") % {"scope": scope}
        return f"{description}\n\n{note}" if description else note

    def get_extensions(self):
        extensions = super().get_extensions()
        if scope := self._required_scope():
            extensions = {**extensions, "x-required-scope": scope}
        return extensions
