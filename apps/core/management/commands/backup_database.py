from django.core.management.base import BaseCommand, CommandError

from apps.core.backups import BackupError, run_backup


class Command(BaseCommand):
    help = "Respalda la base con pg_dump (formato custom) en R2 `backups/` o en BACKUP_DIR."

    def handle(self, *args, **options):
        try:
            name = run_backup()
        except BackupError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(self.style.SUCCESS(f"Respaldo guardado: {name}"))
