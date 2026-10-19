"""Módulo `custom_roles` en Profesional y Empresa (ORG-12, ADR-031).

Como `assistant` (0017): explícito e idempotente. Un plan sin módulos listados no tiene
restricciones y queda como está; Prueba y Básico no lo incluyen.
"""

from django.db import migrations

PLAN_CODES = ("pro", "enterprise")


def add_custom_roles(apps, schema_editor):
    Plan = apps.get_model("organizations", "Plan")
    for plan in Plan.objects.filter(code__in=PLAN_CODES):
        features = list(plan.features or [])
        if features and "custom_roles" not in features:
            plan.features = [*features, "custom_roles"]
            plan.save(update_fields=["features"])


def remove_custom_roles(apps, schema_editor):
    Plan = apps.get_model("organizations", "Plan")
    for plan in Plan.objects.filter(code__in=PLAN_CODES):
        if "custom_roles" in (plan.features or []):
            plan.features = [code for code in plan.features if code != "custom_roles"]
            plan.save(update_fields=["features"])


class Migration(migrations.Migration):
    dependencies = [("organizations", "0017_assistant_module")]

    operations = [migrations.RunPython(add_custom_roles, remove_custom_roles)]
