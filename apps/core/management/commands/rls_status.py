"""Verifica Row-Level Security en las tablas de TenantModel (ADR-026, docs/07_operacion.md).

Uso: python manage.py rls_status [--apply]
Sale con error si alguna tabla de un TenantModel no tiene RLS forzado y la política.
"""

from django.core.management.base import BaseCommand, CommandError
from django.db import connection

from apps.core import rls


class Command(BaseCommand):
    help = "Lista las tablas por organización sin Row-Level Security (solo PostgreSQL)."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Reaplica las políticas antes.")

    def handle(self, *args, apply=False, **options):
        if connection.vendor != "postgresql":
            self.stdout.write("RLS solo existe en PostgreSQL; nada que verificar.")
            return
        if apply:
            rls.apply_policies()
        with connection.cursor() as cursor:
            cursor.execute("SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname = current_user")
            if cursor.fetchone()[0]:
                self.stderr.write("ADVERTENCIA: el rol actual es superusuario o BYPASSRLS; RLS no lo limita.")
        missing = rls.missing_policies()
        if missing:
            raise CommandError("Tablas sin RLS: " + ", ".join(missing))
        self.stdout.write(f"RLS activo en {len(rls.tenant_tables())} tablas por organización.")
