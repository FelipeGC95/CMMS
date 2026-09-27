"""Tareas de adjuntos (Procrastinate, ADR-007): extracción del texto de manuales (AST-06 CA3).

Envoltorio delgado sobre `services.extract_document_text`. Solo se encola con
`DOCUMENT_TEXT_USE_QUEUE`; si no, el servicio extrae al subir (ver
`services.schedule_text_extraction`).
"""

from procrastinate.contrib.django import app

from apps.core.tenancy import tenant_context
from apps.organizations.models import Organization

from . import services
from .models import Attachment, Purpose


@app.task(name="attachments.extract_document_text", queue="documents", retry=2)
def extract_document_text(*, organization_id: str, attachment_id: str) -> str:
    organization = Organization.objects.filter(pk=organization_id, is_active=True).first()
    if organization is None:
        return ""
    with tenant_context(organization):
        attachment = Attachment.objects.filter(pk=attachment_id, purpose=Purpose.MANUAL).first()
        if attachment is None:
            return ""
        return services.extract_document_text(attachment=attachment)
