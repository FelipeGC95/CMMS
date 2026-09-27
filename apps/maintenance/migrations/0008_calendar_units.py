# Frecuencia por calendario en días, semanas o meses (PM-02 CA1).
# Los intervalos existentes eran días: se renombra el campo y la unidad parte en "days".

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("maintenance", "0007_checklist_item_types"),
    ]

    operations = [
        migrations.RenameField(
            model_name="planversion",
            old_name="interval_days",
            new_name="calendar_interval",
        ),
        migrations.AlterField(
            model_name="planversion",
            name="calendar_interval",
            field=models.PositiveIntegerField(blank=True, null=True, verbose_name="cada cuánto (calendario)"),
        ),
        migrations.AddField(
            model_name="planversion",
            name="calendar_unit",
            field=models.CharField(
                choices=[("days", "días"), ("weeks", "semanas"), ("months", "meses")],
                default="days",
                max_length=8,
                verbose_name="unidad",
            ),
        ),
    ]
