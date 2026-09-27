"""Contexto de organización (tenant) activo.

El contexto se fija por petición en `TenantMiddleware` y explícitamente con
`tenant_context()` en comandos, tareas y tests. Sin contexto, los managers de
`TenantModel` fallan cerrado (ver docs/04_arquitectura.md §4).

En PostgreSQL el mismo contexto se replica en variables de sesión de la
conexión (`app.current_organization`, `app.bypass_rls`) que leen las políticas
de Row-Level Security (ADR-026, `apps/core/rls.py`). Cada cambio de contexto
se sincroniza con la base y se deshace al salir, así que el valor nunca
sobrevive a la petición o tarea que lo fijó.
"""

from contextlib import contextmanager
from contextvars import ContextVar

_current_organization = ContextVar("current_organization", default=None)
_bypass_rls = ContextVar("bypass_rls", default=False)

ORGANIZATION_SETTING = "app.current_organization"
BYPASS_SETTING = "app.bypass_rls"


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


def rls_bypass_active() -> bool:
    return _bypass_rls.get()


def apply_rls_settings(connection=None):
    """Copia el contexto actual a las variables de sesión de la conexión (solo PostgreSQL).

    Se ejecuta sobre el cursor nativo para no contar como consulta de la
    aplicación (tests de cantidad de consultas). Si la conexión aún no existe
    y no hay nada que fijar, no la abre: una conexión nueva parte sin valores
    y `connection_created` vuelve a llamar a esta función al abrirla.
    """
    if connection is None:
        from django.db import connection
    if connection.vendor != "postgresql":
        return
    organization = _current_organization.get()
    bypass = _bypass_rls.get()
    raw = connection.connection
    if raw is None:
        if organization is None and not bypass:
            return
        connection.ensure_connection()  # dispara connection_created, que aplica los valores
        return
    from psycopg import pq

    if raw.info.transaction_status == pq.TransactionStatus.INERROR:
        # Transacción abortada: no admite consultas. El ROLLBACK que viene
        # devuelve las variables al valor previo a la transacción.
        return
    with raw.cursor() as cursor:
        cursor.execute(
            "SELECT set_config(%s, %s, false), set_config(%s, %s, false)",
            [
                ORGANIZATION_SETTING,
                "" if organization is None else str(organization.pk),
                BYPASS_SETTING,
                "on" if bypass else "off",
            ],
        )


@contextmanager
def tenant_context(organization):
    token = _current_organization.set(organization)
    apply_rls_settings()
    try:
        yield organization
    finally:
        _current_organization.reset(token)
        apply_rls_settings()


@contextmanager
def platform_context():
    """Operación de plataforma revisada: desactiva RLS mientras dura (ADR-026).

    Úsese solo en comandos de plataforma, migraciones de datos y consultas
    `all_tenants` (que ya lo aplican solas). No cambia el contexto de tenant.
    """
    token = _bypass_rls.set(True)
    apply_rls_settings()
    try:
        yield
    finally:
        _bypass_rls.reset(token)
        apply_rls_settings()
