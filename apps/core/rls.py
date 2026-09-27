"""Row-Level Security de PostgreSQL para las tablas de TenantModel (NF-07, ADR-026).

Cada tabla de un modelo concreto que hereda de `TenantModel` queda con
`ENABLE` + `FORCE ROW LEVEL SECURITY` y la política `tenant_isolation`: solo
se ven y escriben filas cuya `organization_id` es la de la variable de sesión
`app.current_organization`, salvo con `app.bypass_rls = 'on'` (código de
plataforma). Sin variable no se ve ninguna fila: falla cerrado.

Las políticas se (re)aplican de forma idempotente en `post_migrate`, así que
toda tabla nueva de un TenantModel queda cubierta en el mismo `migrate` que la
crea, sin migraciones a mano. `migrate` corre con el bypass activo
(`pre_migrate`) para que las migraciones de datos vean todas las filas.
`missing_policies()` (y `manage.py rls_status`) detecta tablas sin política.
"""

from django.apps import apps
from django.db import connections

from .tenancy import BYPASS_SETTING, ORGANIZATION_SETTING, apply_rls_settings

POLICY_NAME = "tenant_isolation"

_CONDITION = (
    f"current_setting('{BYPASS_SETTING}', true) = 'on' OR "
    f"organization_id = NULLIF(current_setting('{ORGANIZATION_SETTING}', true), '')::uuid"
)


def tenant_tables() -> list[str]:
    """Tablas de todos los TenantModel concretos y gestionados (descubiertas, no listadas)."""
    from .models import TenantModel

    return sorted(
        {
            model._meta.db_table
            for model in apps.get_models()
            if issubclass(model, TenantModel) and model._meta.managed and not model._meta.proxy
        }
    )


def apply_policies(using="default") -> list[str]:
    connection = connections[using]
    if connection.vendor != "postgresql":
        return []
    tables = tenant_tables()
    existing = set(connection.introspection.table_names())
    applied = []
    with connection.cursor() as cursor:
        for table in tables:
            if table not in existing:
                continue
            quoted = connection.ops.quote_name(table)
            cursor.execute(f"ALTER TABLE {quoted} ENABLE ROW LEVEL SECURITY")
            cursor.execute(f"ALTER TABLE {quoted} FORCE ROW LEVEL SECURITY")
            cursor.execute(f"DROP POLICY IF EXISTS {POLICY_NAME} ON {quoted}")
            cursor.execute(
                f"CREATE POLICY {POLICY_NAME} ON {quoted} USING ({_CONDITION}) WITH CHECK ({_CONDITION})"
            )
            applied.append(table)
    return applied


def missing_policies(using="default") -> list[str]:
    """Tablas de TenantModel sin RLS forzado o sin la política (vacío si todo está bien)."""
    connection = connections[using]
    if connection.vendor != "postgresql":
        return []
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT c.relname
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = current_schema()
              AND c.relkind = 'r'
              AND c.relrowsecurity AND c.relforcerowsecurity
              AND EXISTS (
                SELECT 1 FROM pg_policies p
                WHERE p.schemaname = n.nspname AND p.tablename = c.relname AND p.policyname = %s
              )
            """,
            [POLICY_NAME],
        )
        protected = {row[0] for row in cursor.fetchall()}
    return [table for table in tenant_tables() if table not in protected]


# --- Señales (conectadas en CoreConfig.ready) --------------------------------------------


def on_connection_created(sender, connection, **kwargs):
    # Conexión nueva (o reconectada) dentro de un contexto activo: hereda sus valores.
    apply_rls_settings(connection)


def on_request_finished(sender, **kwargs):
    # Tras la petición el contexto ya se cerró: deja la conexión persistente
    # (CONN_MAX_AGE) sin organización ni bypass, pase lo que pase en la vista.
    for connection in connections.all(initialized_only=True):
        apply_rls_settings(connection)


def on_pre_migrate(sender, using="default", **kwargs):
    # Conectada solo para la app core: se emite una vez, antes de cualquier migración.
    connection = connections[using]
    if connection.vendor == "postgresql":
        with connection.cursor() as cursor:
            cursor.execute("SELECT set_config(%s, 'on', false)", [BYPASS_SETTING])


def on_post_migrate(sender, using="default", **kwargs):
    # Conectada solo para la app core: todas las migraciones ya corrieron, así
    # que todas las tablas existen. Después vuelve al contexto (sin bypass).
    connection = connections[using]
    apply_policies(using)
    apply_rls_settings(connection)
