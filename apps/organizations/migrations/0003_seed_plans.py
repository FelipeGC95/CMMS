from django.db import migrations

from apps.organizations.plans import ensure_default_plans


def seed_plans(apps, schema_editor):
    Plan = apps.get_model("organizations", "Plan")
    Organization = apps.get_model("organizations", "Organization")
    ensure_default_plans(Plan)
    # Las organizaciones existentes (demo) quedan en Profesional.
    Organization.objects.filter(plan__isnull=True).update(plan=Plan.objects.get(code="pro"))


class Migration(migrations.Migration):
    dependencies = [("organizations", "0002_plans")]

    operations = [migrations.RunPython(seed_plans, migrations.RunPython.noop)]
