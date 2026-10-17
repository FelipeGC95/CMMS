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
    ASSET_CLASS = "asset_class", _("Clase de activo")
    # Foto de un aviso anónimo por QR (REQ-02) hasta que se acepta; entonces pasa a la OT.
    WORK_REQUEST = "work_request", _("Solicitud")
    # Foto de una pregunta al asistente de diagnóstico (AI-01 CA3): placa, alarma, pieza dañada.
    ASSISTANT_MESSAGE = "assistant_message", _("Mensaje del asistente")


class Purpose(models.TextChoices):
    PHOTO = "photo", _("Foto")
    DOCUMENT = "document", _("Documento")
    SIGNATURE = "signature", _("Firma")
    MANUAL = "manual", _("Manual")


class TextStatus(models.TextChoices):
    """Extracción del texto de un manual (AST-06 CA3); en blanco para los demás adjuntos."""

    NOT_APPLICABLE = "", _("No aplica")
    PENDING = "pending", _("Texto pendiente")
    DONE = "done", _("Texto listo")
    NO_TEXT = "no_text", _("Sin texto (escaneado)")
    FAILED = "failed", _("No se pudo leer")


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
    sha256 = models.CharField(max_length=64, blank=True, verbose_name=_("SHA-256"))
    uploaded_by = models.ForeignKey(
        "accounts.User", on_delete=models.SET_NULL, null=True, related_name="+", verbose_name=_("subido por")
    )
    client_uuid = models.UUIDField(null=True, blank=True, verbose_name=_("UUID del cliente"))
    text_status = models.CharField(
        _("estado del texto"), max_length=10, choices=TextStatus.choices, default="", blank=True
    )
    page_count = models.PositiveIntegerField(_("páginas"), null=True, blank=True)

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


class DocumentPage(TenantModel):
    """Texto de una página de un manual PDF (AST-06 CA3), para la búsqueda y el asistente (ADR-018).

    Lo llena la extracción en segundo plano (`services.extract_document_text`);
    se borra junto con el adjunto.
    """

    attachment = models.ForeignKey(
        Attachment, on_delete=models.CASCADE, related_name="pages", verbose_name=_("adjunto")
    )
    page_number = models.PositiveIntegerField(_("página"))
    text = models.TextField(_("texto"))

    class Meta:
        verbose_name = _("página de documento")
        verbose_name_plural = _("páginas de documento")
        ordering = ["attachment", "page_number"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "attachment", "page_number"], name="uniq_document_page"
            ),
        ]

    def __str__(self):
        return f"{self.attachment} · {self.page_number}"
