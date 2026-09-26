"""Documentos y fotos, referenciados de forma genérica (AST-06, docs/03_modelo_de_datos.md § 2.7).

`target_type` + `target_id` en vez de `GenericForeignKey`, para mantener
simple la exportación (ver el modelo de datos). Cada app dueña del target
(work_order, asset…) verifica el alcance antes de crear o servir un adjunto.
"""

import uuid
from pathlib import Path

from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.core.models import TenantModel


def attachment_upload_to(instance, filename):
    ext = Path(filename).suffix.lower()[:10]
    return f"org/{instance.organization_id}/{uuid.uuid4().hex}{ext}"


class TargetType(models.TextChoices):
    ASSET = "asset", _("Activo")
    WORK_ORDER = "work_order", _("Orden de trabajo")
    CHECKLIST_ITEM = "checklist_item", _("Ítem de checklist")


class Purpose(models.TextChoices):
    PHOTO = "photo", _("Foto")
    DOCUMENT = "document", _("Documento")
    SIGNATURE = "signature", _("Firma")


class Attachment(TenantModel):
    target_type = models.CharField(_("tipo de objeto"), max_length=20, choices=TargetType.choices)
    target_id = models.UUIDField(_("id del objeto"))
    purpose = models.CharField(
        _("propósito"), max_length=20, choices=Purpose.choices, default=Purpose.DOCUMENT
    )
    file = models.FileField(_("archivo"), upload_to=attachment_upload_to)
    filename = models.CharField(_("nombre"), max_length=200)
    content_type = models.CharField(_("tipo MIME"), max_length=100)
    size_bytes = models.PositiveIntegerField(_("tamaño (bytes)"))
    # Solo para imágenes; en blanco para documentos (AST-06 CA1).
    width = models.PositiveIntegerField(_("ancho (px)"), null=True, blank=True)
    height = models.PositiveIntegerField(_("alto (px)"), null=True, blank=True)
    sha256 = models.CharField(max_length=64, blank=True)
    uploaded_by = models.ForeignKey("accounts.User", on_delete=models.SET_NULL, null=True, related_name="+")
    client_uuid = models.UUIDField(null=True, blank=True)

    class Meta:
        verbose_name = _("adjunto")
        verbose_name_plural = _("adjuntos")
        ordering = ["created_at"]
        indexes = [
            models.Index(fields=["organization", "target_type", "target_id"], name="attachment_target_idx"),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "client_uuid"], name="uniq_attachment_client_uuid"
            ),
        ]

    def __str__(self):
        return self.filename
