"""Las fotos de avisos anónimos por QR (REQ-02) pasan a `target_type=work_request`.

Antes se guardaban como `work_order` con el id de la solicitud hasta aceptarla.
Una fila se convierte solo si su `target_id` es una solicitud de la misma
organización y no es una OT. Usa modelos históricos (sin TenantManager); el
bypass de RLS lo activa `pre_migrate` (ADR-026), así que se ven todas las filas.
"""

from django.db import migrations, models


def _anonymous_photos(apps, *, from_type):
    Attachment = apps.get_model("attachments", "Attachment")
    WorkRequest = apps.get_model("work", "WorkRequest")
    WorkOrder = apps.get_model("work", "WorkOrder")
    return Attachment.objects.filter(target_type=from_type, purpose="photo").filter(
        models.Exists(
            WorkRequest.objects.filter(
                pk=models.OuterRef("target_id"), organization_id=models.OuterRef("organization_id")
            )
        ),
        ~models.Exists(WorkOrder.objects.filter(pk=models.OuterRef("target_id"))),
    )


def forwards(apps, schema_editor):
    _anonymous_photos(apps, from_type="work_order").update(target_type="work_request")


def backwards(apps, schema_editor):
    _anonymous_photos(apps, from_type="work_request").update(target_type="work_order")


class Migration(migrations.Migration):
    dependencies = [
        ("attachments", "0004_manuals_and_document_pages"),
        ("work", "0014_work_request_anonymous"),
    ]

    operations = [
        migrations.AlterField(
            model_name="attachment",
            name="target_type",
            field=models.CharField(
                choices=[
                    ("asset", "Activo"),
                    ("work_order", "Orden de trabajo"),
                    ("checklist_item", "Ítem de checklist"),
                    ("asset_class", "Clase de activo"),
                    ("work_request", "Solicitud"),
                ],
                max_length=20,
                verbose_name="tipo de objeto",
            ),
        ),
        migrations.RunPython(forwards, backwards),
    ]
