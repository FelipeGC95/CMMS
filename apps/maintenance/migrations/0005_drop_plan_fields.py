"""Quita del plan los campos que pasaron a la versión (ADR-016).

Reversa: no se puede recrear sin pérdida el FK obligatorio PlanTaskItem.plan
ni los campos del plan para versiones posteriores a la 1; revertir más allá
de esta migración requiere restaurar un respaldo.
"""

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("maintenance", "0004_backfill_versions")]

    operations = [
        migrations.RemoveField(model_name="maintenanceplan", name="estimated_minutes"),
        migrations.RemoveField(model_name="maintenanceplan", name="interval_days"),
        migrations.RemoveField(model_name="maintenanceplan", name="meter_interval"),
        migrations.RemoveField(model_name="maintenanceplan", name="meter_kind"),
        migrations.RemoveField(model_name="plantaskitem", name="plan"),
        migrations.AlterField(
            model_name="plantaskitem",
            name="version",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="items",
                to="maintenance.planversion",
            ),
        ),
        migrations.AlterField(
            model_name="maintenanceplan",
            name="current_version",
            field=models.ForeignKey(
                blank=True,
                editable=False,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="maintenance.planversion",
                verbose_name="versión vigente",
            ),
        ),
    ]
