from django.db import migrations


def verify_existing(apps, schema_editor):
    """Los usuarios creados antes de allauth los dio de alta la plataforma: su correo es confiable."""
    User = apps.get_model("accounts", "User")
    EmailAddress = apps.get_model("account", "EmailAddress")
    for user in User.objects.exclude(pk__in=EmailAddress.objects.values("user_id")):
        EmailAddress.objects.create(user=user, email=user.email.lower(), primary=True, verified=True)


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0002_user_last_organization"),
        ("account", "0009_emailaddress_unique_primary_email"),
    ]

    operations = [migrations.RunPython(verify_existing, migrations.RunPython.noop)]
