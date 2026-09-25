"""Cada plan existente pasa a tener una versión 1 publicada (ADR-016).

Copia frecuencia, duración y checklist del plan a la versión, apunta
`current_version` a ella y mueve los ítems. Usa modelos históricos
(`apps.get_model`): no tienen TenantManager ni el save() de TenantModel, así
que organization_id e id (UUIDv7) se fijan explícitamente.
"""

from django.db import migrations
from django.utils import timezone

from apps.core.models import uuid7


def forwards(apps, schema_editor):
    Plan = apps.get_model("maintenance", "MaintenancePlan")
    PlanVersion = apps.get_model("maintenance", "PlanVersion")
    PlanTaskItem = apps.get_model("maintenance", "PlanTaskItem")
    now = timezone.now()
    for plan in Plan.objects.all().iterator():
        version = PlanVersion.objects.create(
            id=uuid7(),
            organization_id=plan.organization_id,
            plan_id=plan.id,
            number=1,
            status="published",
            published_at=now,
            change_note="Versión inicial (migrada)",
            estimated_minutes=plan.estimated_minutes,
            interval_days=plan.interval_days,
            meter_kind=plan.meter_kind,
            meter_interval=plan.meter_interval,
        )
        PlanTaskItem.objects.filter(plan_id=plan.id).update(version_id=version.id)
        Plan.objects.filter(id=plan.id).update(current_version_id=version.id)


def backwards(apps, schema_editor):
    """Devuelve la frecuencia y el checklist de la versión vigente al plan."""
    Plan = apps.get_model("maintenance", "MaintenancePlan")
    PlanVersion = apps.get_model("maintenance", "PlanVersion")
    PlanTaskItem = apps.get_model("maintenance", "PlanTaskItem")
    for plan in Plan.objects.exclude(current_version_id=None).iterator():
        version = PlanVersion.objects.get(id=plan.current_version_id)
        Plan.objects.filter(id=plan.id).update(
            estimated_minutes=version.estimated_minutes,
            interval_days=version.interval_days,
            meter_kind=version.meter_kind,
            meter_interval=version.meter_interval,
            current_version_id=None,
        )
        PlanTaskItem.objects.filter(version_id=version.id).update(plan_id=plan.id)
    PlanTaskItem.objects.update(version_id=None)
    PlanVersion.objects.all().delete()


class Migration(migrations.Migration):
    dependencies = [("maintenance", "0003_plan_versions")]

    operations = [migrations.RunPython(forwards, backwards)]
