"""Respaldo lógico de la base (NF-09, docs/07_operacion.md).

`pg_dump` en formato custom (se restaura con `pg_restore`). El archivo va a
`backups/` del bucket R2 cuando está configurado, o a BACKUP_DIR en disco. No
es un dato de ninguna organización: nunca queda bajo `org/`.
"""

import datetime
import logging
import os
import subprocess
import tempfile
from pathlib import Path

from django.conf import settings
from django.core.files import File
from django.core.files.storage import FileSystemStorage, default_storage
from django.db import connections
from django.utils import timezone

from config.infra import r2_enabled

logger = logging.getLogger(__name__)

PREFIX = "backups"
NAME_FORMAT = "cmms-%Y%m%dT%H%M%SZ.dump"


class BackupError(RuntimeError):
    pass


def backup_storage():
    """R2 (el mismo bucket privado, prefijo `backups/`) o un directorio local."""
    if r2_enabled(os.environ):
        return default_storage
    return FileSystemStorage(location=settings.BACKUP_DIR)


def _pg_env(db: dict) -> dict:
    env = dict(os.environ)
    mapping = {"PGHOST": "HOST", "PGPORT": "PORT", "PGUSER": "USER", "PGPASSWORD": "PASSWORD"}
    for var, key in mapping.items():
        if db.get(key):
            env[var] = str(db[key])
    if sslmode := (db.get("OPTIONS") or {}).get("sslmode"):
        env["PGSSLMODE"] = sslmode
    # Row-Level Security (ADR-026): el dueño de las tablas también queda sujeto
    # (FORCE). El respaldo es de plataforma: bypass de la política para ver todo.
    env["PGOPTIONS"] = (env.get("PGOPTIONS", "") + " -c app.bypass_rls=on").strip()
    return env


def _backup_name(now: datetime.datetime) -> str:
    return f"{PREFIX}/{now.astimezone(datetime.UTC).strftime(NAME_FORMAT)}"


def dump_database(target: Path, *, alias: str = "default") -> None:
    db = connections[alias].settings_dict
    if connections[alias].vendor != "postgresql":
        raise BackupError("backup_database requiere PostgreSQL (pg_dump).")
    command = [
        "pg_dump",
        "--format=custom",
        "--no-owner",
        "--no-acl",
        "--enable-row-security",  # sin esto pg_dump falla en tablas con RLS forzado
        f"--file={target}",
        db["NAME"],
    ]
    try:
        result = subprocess.run(command, env=_pg_env(db), capture_output=True, text=True, check=False)
    except FileNotFoundError as exc:
        raise BackupError("No se encontró pg_dump en el PATH.") from exc
    if result.returncode != 0:
        raise BackupError(f"pg_dump falló ({result.returncode}): {result.stderr.strip()}")


def run_backup(*, now: datetime.datetime | None = None, storage=None) -> str:
    """Genera el respaldo, lo guarda y aplica la retención. Devuelve el nombre guardado."""
    now = now or timezone.now()
    storage = storage or backup_storage()
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "backup.dump"
        dump_database(target)
        with target.open("rb") as fh:
            name = storage.save(_backup_name(now), File(fh))
    logger.info("Respaldo guardado: %s", name)
    prune_backups(now=now, storage=storage)
    return name


def prune_backups(*, now: datetime.datetime, storage, keep_days: int | None = None) -> list[str]:
    """Borra respaldos más antiguos que la retención (BACKUP_RETENTION_DAYS)."""
    keep_days = settings.BACKUP_RETENTION_DAYS if keep_days is None else keep_days
    cutoff = now - datetime.timedelta(days=keep_days)
    try:
        _, files = storage.listdir(PREFIX)
    except FileNotFoundError:
        return []
    removed = []
    for filename in files:
        try:
            taken = datetime.datetime.strptime(filename, NAME_FORMAT).replace(tzinfo=datetime.UTC)
        except ValueError:
            continue  # archivos ajenos al patrón no se tocan
        if taken < cutoff:
            storage.delete(f"{PREFIX}/{filename}")
            removed.append(filename)
    return removed
