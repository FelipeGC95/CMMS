"""Módulo `condition` en los planes Prueba, Profesional y Empresa existentes (CBM-08, D4).

Igual que `inventory` (0014): explícito e idempotente; un plan sin módulos
listados no tiene restricciones y queda como está; Básico no lo incluye.
"""

from django.db import migrations

CONDITION_PLAN_CODES = ("trial", "pro", "enterprise")


def add_condition(apps, schema_editor):
    Plan = apps.get_model("organizations", "Plan")
    for plan in Plan.objects.filter(code__in=CONDITION_PLAN_CODES):
        features = list(plan.features or [])
        if features and "condition" not in features:
            plan.features = [*features, "condition"]
            plan.save(update_fields=["features"])


def remove_condition(apps, schema_editor):
    Plan = apps.get_model("organizations", "Plan")
    for plan in Plan.objects.filter(code__in=CONDITION_PLAN_CODES):
        if "condition" in (plan.features or []):
            plan.features = [code for code in plan.features if code != "condition"]
            plan.save(update_fields=["features"])


class Migration(migrations.Migration):
    dependencies = [("organizations", "0014_inventory_module")]

    operations = [migrations.RunPython(add_condition, remove_condition)]
