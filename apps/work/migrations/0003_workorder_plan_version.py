"""La OT guarda la versión del plan con que se generó (ADR-016).

Las OT preventivas existentes quedan apuntando a la versión vigente de su
plan, que tras maintenance.0004 es la versión 1 migrada. Usa modelos
históricos; la reversa solo deja el campo en blanco (se elimina luego).
"""

import django.db.models.deletion
from django.db import migrations, models


def backfill(apps, schema_editor):
    WorkOrder = apps.get_model("work", "WorkOrder")
    PlanAssignment = apps.get_model("maintenance", "PlanAssignment")
    Plan = apps.get_model("maintenance", "MaintenancePlan")
    current = dict(Plan.objects.values_list("id", "current_version_id"))
    plan_of = dict(PlanAssignment.objects.values_list("id", "plan_id"))
    pending = WorkOrder.objects.filter(plan_assignment_id__isnull=False, plan_version_id__isnull=True)
    for work_order_id, assignment_id in pending.values_list("id", "plan_assignment_id").iterator():
        version_id = current.get(plan_of.get(assignment_id))
        if version_id:
            WorkOrder.objects.filter(id=work_order_id).update(plan_version_id=version_id)


class Migration(migrations.Migration):
    dependencies = [
        ("maintenance", "0005_drop_plan_fields"),
        ("work", "0002_meter_fields"),
    ]

    operations = [
        migrations.AddField(
            model_name="workorder",
            name="plan_version",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="work_orders",
                to="maintenance.planversion",
            ),
        ),
        migrations.RunPython(backfill, migrations.RunPython.noop),
    ]
