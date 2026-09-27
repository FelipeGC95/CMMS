"""Respaldo lógico (NF-09): pg_dump simulado, almacenamiento local temporal."""

import datetime
import subprocess

import pytest
from django.core.files.base import ContentFile
from django.core.files.storage import FileSystemStorage
from django.core.management import CommandError, call_command
from django.db import connections

from apps.core import backups

NOW = datetime.datetime(2026, 9, 27, 7, 0, tzinfo=datetime.UTC)


@pytest.fixture
def fake_pg_dump(monkeypatch):
    calls = []

    def run(command, env, **kwargs):
        calls.append((command, env))
        target = next(arg.split("=", 1)[1] for arg in command if arg.startswith("--file="))
        with open(target, "wb") as fh:
            fh.write(b"PGDMP-fake")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(backups.subprocess, "run", run)
    monkeypatch.setattr(type(connections["default"]), "vendor", "postgresql", raising=False)
    return calls


def test_backup_runs_pg_dump_custom_format_and_stores_under_backups(tmp_path, fake_pg_dump, db):
    storage = FileSystemStorage(location=tmp_path)
    name = backups.run_backup(now=NOW, storage=storage)

    assert name == "backups/cmms-20260927T070000Z.dump"
    assert (tmp_path / name).read_bytes() == b"PGDMP-fake"
    command, _ = fake_pg_dump[0]
    assert command[0] == "pg_dump"
    assert "--format=custom" in command
    assert "--enable-row-security" in command
    assert "app.bypass_rls=on" in fake_pg_dump[0][1]["PGOPTIONS"]
    assert not name.startswith("org/")


def test_old_backups_are_pruned_and_foreign_files_kept(tmp_path, settings):
    settings.BACKUP_RETENTION_DAYS = 30
    storage = FileSystemStorage(location=tmp_path)
    for day in (1, 20, 40):
        taken = NOW - datetime.timedelta(days=day)
        storage.save(f"backups/{taken.strftime(backups.NAME_FORMAT)}", ContentFile(b"x"))
    storage.save("backups/notas.txt", ContentFile(b"x"))

    removed = backups.prune_backups(now=NOW, storage=storage)

    assert removed == [(NOW - datetime.timedelta(days=40)).strftime(backups.NAME_FORMAT)]
    assert len(storage.listdir("backups")[1]) == 3


def test_pg_dump_failure_is_reported(monkeypatch, tmp_path, db):
    monkeypatch.setattr(type(connections["default"]), "vendor", "postgresql", raising=False)
    monkeypatch.setattr(
        backups.subprocess,
        "run",
        lambda command, env, **kw: subprocess.CompletedProcess(command, 1, "", "connection refused"),
    )
    with pytest.raises(backups.BackupError, match="connection refused"):
        backups.run_backup(now=NOW, storage=FileSystemStorage(location=tmp_path))


def test_command_writes_to_backup_dir(tmp_path, settings, fake_pg_dump, db, monkeypatch):
    settings.BACKUP_DIR = tmp_path
    for var in ("R2_BUCKET", "R2_ENDPOINT_URL", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"):
        monkeypatch.delenv(var, raising=False)
    call_command("backup_database")
    assert len(list((tmp_path / "backups").iterdir())) == 1


def test_command_fails_cleanly_without_pg_dump(monkeypatch, tmp_path, settings, db):
    settings.BACKUP_DIR = tmp_path
    monkeypatch.setattr(type(connections["default"]), "vendor", "postgresql", raising=False)

    def missing(*args, **kwargs):
        raise FileNotFoundError

    monkeypatch.setattr(backups.subprocess, "run", missing)
    with pytest.raises(CommandError, match="pg_dump"):
        call_command("backup_database")
