"""Contexto de organización (tenant) activo.

El contexto se fija por petición en `TenantMiddleware` y explícitamente con
`tenant_context()` en comandos, tareas y tests. Sin contexto, los managers de
`TenantModel` fallan cerrado (ver docs/04_arquitectura.md §4).
"""

from contextlib import contextmanager
from contextvars import ContextVar

_current_organization = ContextVar("current_organization", default=None)


class TenantNotSet(RuntimeError):
    """Se intentó acceder a datos de negocio sin organización activa."""


class CrossTenantError(RuntimeError):
    """Se intentó escribir o relacionar datos de otra organización."""


def get_current_organization():
    return _current_organization.get()


def require_current_organization():
    organization = _current_organization.get()
    if organization is None:
        raise TenantNotSet("No hay organización activa en el contexto.")
    return organization


@contextmanager
def tenant_context(organization):
    token = _current_organization.set(organization)
    try:
        yield organization
    finally:
        _current_organization.reset(token)
