import django.core.validators
from django.db import migrations, models
from django.db.models.functions import ExtractDay


def fill_anchor_day(apps, schema_editor):
    # Mejor esfuerzo: el día del vencimiento guardado (si ya se ajustó a fin de mes, queda ese día).
    PlanAssignment = apps.get_model("maintenance", "PlanAssignment")
    PlanAssignment.objects.filter(next_due_on__isnull=False).update(anchor_day=ExtractDay("next_due_on"))


class Migration(migrations.Migration):
    dependencies = [("maintenance", "0010_plan_absorption")]

    operations = [
        migrations.AddField(
            model_name="planassignment",
            name="anchor_day",
            field=models.PositiveSmallIntegerField(
                blank=True,
                null=True,
                validators=[
                    django.core.validators.MinValueValidator(1),
                    django.core.validators.MaxValueValidator(31),
                ],
                verbose_name="día ancla",
            ),
        ),
        migrations.RunPython(fill_anchor_day, migrations.RunPython.noop),
    ]
