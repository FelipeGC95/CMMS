from django.core.management import call_command
from django.db import migrations


def create_cache_table(apps, schema_editor):
    # Tabla de CACHES (límites de intentos de allauth); idempotente.
    call_command("createcachetable", database=schema_editor.connection.alias, verbosity=0)


class Migration(migrations.Migration):
    dependencies = [("accounts", "0003_verified_email_for_existing_users")]

    operations = [migrations.RunPython(create_cache_table, migrations.RunPython.noop)]
