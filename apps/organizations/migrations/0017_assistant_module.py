"""Módulo `assistant` y cupo mensual de conversaciones en Profesional y Empresa (AI-05, D2).

Como `inventory` (0014) y `condition` (0015): explícito e idempotente. Un plan sin módulos
listados no tiene restricciones y queda como está; Prueba y Básico no lo incluyen. El cupo
solo se fija si está en blanco (no pisa uno editado en el admin).
"""

from django.db import migrations

ASSISTANT_QUOTAS = {"pro": 100, "enterprise": 500}


def add_assistant(apps, schema_editor):
    Plan = apps.get_model("organizations", "Plan")
    for plan in Plan.objects.filter(code__in=ASSISTANT_QUOTAS):
        fields = []
        features = list(plan.features or [])
        if features and "assistant" not in features:
            plan.features = [*features, "assistant"]
            fields.append("features")
        if plan.max_assistant_conversations is None:
            plan.max_assistant_conversations = ASSISTANT_QUOTAS[plan.code]
            fields.append("max_assistant_conversations")
        if fields:
            plan.save(update_fields=fields)


def remove_assistant(apps, schema_editor):
    Plan = apps.get_model("organizations", "Plan")
    for plan in Plan.objects.filter(code__in=ASSISTANT_QUOTAS):
        if "assistant" in (plan.features or []):
            plan.features = [code for code in plan.features if code != "assistant"]
            plan.save(update_fields=["features"])


class Migration(migrations.Migration):
    dependencies = [("organizations", "0016_plan_max_assistant_conversations")]

    operations = [migrations.RunPython(add_assistant, remove_assistant)]
