"""Módulo `inventory` en los planes Prueba, Profesional y Empresa existentes (INV-16, Q4).

`ensure_default_plans` no toca planes ya creados: aquí se agrega de forma
explícita e idempotente (sin duplicar). Un plan sin módulos listados no tiene
restricciones y queda como está; Básico no lo incluye.
"""

from django.db import migrations

INVENTORY_PLAN_CODES = ("trial", "pro", "enterprise")


def add_inventory(apps, schema_editor):
    Plan = apps.get_model("organizations", "Plan")
    for plan in Plan.objects.filter(code__in=INVENTORY_PLAN_CODES):
        features = list(plan.features or [])
        if features and "inventory" not in features:
            plan.features = [*features, "inventory"]
            plan.save(update_fields=["features"])


def remove_inventory(apps, schema_editor):
    Plan = apps.get_model("organizations", "Plan")
    for plan in Plan.objects.filter(code__in=INVENTORY_PLAN_CODES):
        if "inventory" in (plan.features or []):
            plan.features = [code for code in plan.features if code != "inventory"]
            plan.save(update_fields=["features"])


class Migration(migrations.Migration):
    dependencies = [("organizations", "0013_data_export_and_deletion")]

    operations = [migrations.RunPython(add_inventory, remove_inventory)]
