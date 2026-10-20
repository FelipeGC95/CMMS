"""Alcances por recurso (X-05, ADR-034): el alcance general pasa a una lista de alcances.

"read" → ["*:read"] y "write" → ["*:read", "*:write"]: los tokens existentes siguen
permitiendo exactamente lo mismo (también en los grupos de endpoints que se agreguen).
"""

from django.db import migrations, models


def to_scopes(apps, schema_editor):
    ApiToken = apps.get_model("api", "ApiToken")
    ApiToken.objects.filter(scope="write").update(scopes=["*:read", "*:write"])
    ApiToken.objects.exclude(scope="write").update(scopes=["*:read"])


def to_scope(apps, schema_editor):
    ApiToken = apps.get_model("api", "ApiToken")
    for token in ApiToken.objects.all():
        token.scope = "write" if any(s.endswith(":write") for s in token.scopes) else "read"
        token.save(update_fields=["scope"])


class Migration(migrations.Migration):
    dependencies = [("api", "0002_idempotency_key")]

    operations = [
        migrations.AddField(
            model_name="apitoken",
            name="scopes",
            field=models.JSONField(default=list, verbose_name="alcances"),
        ),
        migrations.RunPython(to_scopes, to_scope),
        migrations.RemoveField(model_name="apitoken", name="scope"),
    ]
