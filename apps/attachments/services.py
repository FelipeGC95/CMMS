"""Alta de adjuntos: valida tamaño/tipo, comprime imágenes (AST-06 CA1) y respeta el cupo (CA2)."""

import hashlib
import io
import logging
from pathlib import Path

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.db import transaction
from django.template.defaultfilters import filesizeformat
from django.utils.translation import gettext as _
from PIL import Image, ImageOps, UnidentifiedImageError
from pypdf import PdfReader

from apps.audit import services as audit
from apps.audit.models import AuditEvent
from apps.core.tenancy import require_current_organization
from apps.organizations.models import Organization

from .models import Attachment, DocumentPage, Purpose, TargetType, TextStatus
from .selectors import storage_usage

logger = logging.getLogger(__name__)

MAX_IMAGE_DIMENSION = 2000
JPEG_QUALITY = 82
IMAGE_CONTENT_TYPES = {"image/jpeg", "image/png", "image/webp", "image/heic"}
ALLOWED_UPLOAD_TYPES = IMAGE_CONTENT_TYPES | {"application/pdf"}


class AttachmentError(ValidationError):
    pass


class QuotaExceededError(AttachmentError):
    """El archivo es válido pero no cabe en el cupo del plan (AST-06 CA2)."""


def _compress_image(uploaded_file):
    """Reduce el lado mayor a ≤2000 px, respeta la orientación EXIF y descarta el resto de los metadatos
    (incluida la ubicación GPS) al no volver a escribirlos en el archivo de salida.
    """
    try:
        image = Image.open(uploaded_file)
        image.load()
    except (UnidentifiedImageError, Image.DecompressionBombError) as error:
        raise AttachmentError(
            _(
                "No se pudo leer la imagen (formato no compatible, por ejemplo HEIC en algunos equipos). "
                "Conviértela a JPEG o PNG e inténtalo de nuevo."
            )
        ) from error
    image = ImageOps.exif_transpose(image)
    if image.mode in ("RGBA", "LA", "P"):
        # JPEG no tiene transparencia: se aplana sobre blanco (una firma PNG
        # transparente quedaría negra sobre negro con un convert directo).
        image = image.convert("RGBA")
        background = Image.new("RGB", image.size, "white")
        background.paste(image, mask=image.getchannel("A"))
        image = background
    elif image.mode not in ("RGB", "L"):
        image = image.convert("RGB")
    width, height = image.size
    scale = min(1.0, MAX_IMAGE_DIMENSION / max(width, height))
    if scale < 1.0:
        image = image.resize((round(width * scale), round(height * scale)), Image.LANCZOS)
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=JPEG_QUALITY, optimize=True)
    return buffer.getvalue(), image.width, image.height


def add_attachment(
    *,
    target_type: str,
    target_id,
    uploaded_file,
    by,
    purpose: str = Purpose.DOCUMENT,
    client_uuid=None,
) -> Attachment:
    if client_uuid:
        existing = Attachment.objects.filter(client_uuid=client_uuid).first()
        if existing:
            return existing

    if uploaded_file.size > settings.MAX_UPLOAD_BYTES:
        raise AttachmentError(_("El archivo supera el tamaño máximo (20 MB)."))
    content_type = uploaded_file.content_type
    if content_type not in ALLOWED_UPLOAD_TYPES:
        raise AttachmentError(_("Tipo de archivo no permitido."))

    filename = uploaded_file.name[:200]
    width = height = None
    if content_type in IMAGE_CONTENT_TYPES:
        data, width, height = _compress_image(uploaded_file)
        content_type = "image/jpeg"
        filename = str(Path(filename).with_suffix(".jpg"))
    else:
        data = uploaded_file.read()

    with transaction.atomic():
        return _save_within_quota(
            data=data,
            target_type=target_type,
            target_id=target_id,
            purpose=purpose,
            filename=filename,
            content_type=content_type,
            width=width,
            height=height,
            by=by,
            client_uuid=client_uuid,
        )


def _save_within_quota(
    *, data, target_type, target_id, purpose, filename, content_type, width, height, by, client_uuid
) -> Attachment:
    """Guarda el adjunto si cabe en el cupo del plan (AST-06 CA2, ORG-10 CA1).

    Se mide el tamaño ya comprimido. La fila de la organización se bloquea para que dos
    subidas simultáneas no superen el cupo entre ambas.
    """
    organization = require_current_organization()
    Organization.objects.select_for_update().filter(pk=organization.pk).values_list("pk").get()
    usage = storage_usage(organization=organization)
    if not usage.allows(len(data)):
        raise QuotaExceededError(
            _(
                "No hay espacio para este archivo (%(size)s): la organización usa %(used)s de "
                "%(quota)s de su plan. Pide al administrador ampliar el plan."
            )
            % {
                "size": filesizeformat(len(data)),
                "used": filesizeformat(usage.used_bytes),
                "quota": filesizeformat(usage.quota_bytes),
            }
        )
    attachment = Attachment(
        # La organización se fija antes de guardar el archivo: su ruta es org/<organization_id>/….
        organization=organization,
        target_type=target_type,
        target_id=target_id,
        purpose=purpose,
        filename=filename,
        content_type=content_type,
        size_bytes=len(data),
        width=width,
        height=height,
        sha256=hashlib.sha256(data).hexdigest(),
        uploaded_by=by,
        client_uuid=client_uuid,
    )
    attachment.file.save(filename, ContentFile(data), save=False)
    attachment.save()
    return attachment


def remove_attachment(*, attachment: Attachment, by) -> None:
    """Borra un adjunto y su archivo; libera su espacio del cupo (AST-06 CA2, ORG-10).

    Solo para adjuntos que aún son borrador (la app dueña decide cuándo, p. ej.
    fotos de un ítem mientras la OT está abierta): el historial no se borra.
    La auditoría guarda nombre, tamaño y huella; el archivo se elimina recién
    al confirmar la transacción, para no perderlo si esta se revierte.
    """
    audit.record(
        action=AuditEvent.Action.DELETE,
        target=attachment,
        actor=by,
        changes={
            "filename": [attachment.filename, None],
            "size_bytes": [attachment.size_bytes, None],
            "sha256": [attachment.sha256, None],
            "target_id": [str(attachment.target_id), None],
        },
    )
    storage, name = attachment.file.storage, attachment.file.name
    attachment.delete()
    if name:
        transaction.on_commit(lambda: storage.delete(name))


# --- Manuales (AST-06 CA3) ---------------------------------------------------------------

MAX_EXTRACTED_PAGES = 500
MAX_PAGE_CHARS = 20_000
PDF_CONTENT_TYPE = "application/pdf"
MANUAL_TARGETS = {TargetType.ASSET, TargetType.ASSET_CLASS}


def add_manual(*, target_type: str, target_id, uploaded_file, by, client_uuid=None) -> Attachment:
    """Sube un manual PDF a un activo o a una clase de activo y programa la extracción del texto.

    El cupo y el tamaño máximo son los de cualquier adjunto (CA1, CA2). Idempotente por `client_uuid`.
    """
    if target_type not in MANUAL_TARGETS:
        raise AttachmentError(_("Los manuales se adjuntan a un activo o a una clase de activo."))
    head = uploaded_file.read(5)
    uploaded_file.seek(0)
    if uploaded_file.content_type != PDF_CONTENT_TYPE or head != b"%PDF-":
        raise AttachmentError(_("El manual debe ser un archivo PDF."))
    with transaction.atomic():
        attachment = add_attachment(
            target_type=target_type,
            target_id=target_id,
            uploaded_file=uploaded_file,
            by=by,
            purpose=Purpose.MANUAL,
            client_uuid=client_uuid,
        )
        if attachment.text_status == TextStatus.NOT_APPLICABLE:
            attachment.text_status = TextStatus.PENDING
            attachment.save(update_fields=["text_status", "updated_at"])
            audit.record(action=AuditEvent.Action.CREATE, target=attachment, actor=by)
            schedule_text_extraction(attachment=attachment)
    return attachment


def schedule_text_extraction(*, attachment: Attachment) -> None:
    """Extrae el texto al confirmar la transacción.

    Regla: con `DOCUMENT_TEXT_USE_QUEUE` (PostgreSQL + worker encendido) se encola la
    tarea de Procrastinate; si no (SQLite, demo sin worker), se extrae en la misma
    petición. `manage.py extract_document_text` procesa los que queden pendientes.
    """
    organization_id, attachment_id = str(attachment.organization_id), str(attachment.pk)
    if settings.DOCUMENT_TEXT_USE_QUEUE:
        from .tasks import extract_document_text as task

        transaction.on_commit(
            lambda: task.defer(organization_id=organization_id, attachment_id=attachment_id)
        )
    else:
        transaction.on_commit(lambda: extract_document_text(attachment=attachment))


def _clean(text: str) -> str:
    # PostgreSQL no admite NUL en texto; se limita el largo por página.
    return (text or "").replace("\x00", "").strip()[:MAX_PAGE_CHARS]


def extract_document_text(*, attachment: Attachment) -> str:
    """Guarda el texto de cada página del PDF (hasta 500) y deja el estado (AST-06 CA3).

    Un PDF escaneado sin capa de texto queda `no_text`; uno ilegible, `failed`.
    Repetirla reemplaza las páginas: es idempotente.
    """
    try:
        with attachment.file.open("rb") as handle:
            reader = PdfReader(io.BytesIO(handle.read()))
            if reader.is_encrypted:
                reader.decrypt("")
            page_count = len(reader.pages)
            limit = min(page_count, MAX_EXTRACTED_PAGES)
            texts = [_clean(reader.pages[i].extract_text()) for i in range(limit)]
    except Exception:  # pypdf lanza muchos tipos distintos ante un PDF dañado o cifrado.
        logger.warning("No se pudo extraer el texto del adjunto %s", attachment.pk, exc_info=True)
        Attachment.objects.filter(pk=attachment.pk).update(text_status=TextStatus.FAILED)
        attachment.text_status = TextStatus.FAILED
        return attachment.text_status
    pages = [
        # bulk_create no pasa por save(): la organización se fija aquí.
        DocumentPage(
            organization_id=attachment.organization_id, attachment=attachment, page_number=number, text=text
        )
        for number, text in enumerate(texts, start=1)
        if text
    ]
    with transaction.atomic():
        DocumentPage.objects.filter(attachment=attachment).delete()
        DocumentPage.objects.bulk_create(pages)
        attachment.page_count = page_count
        attachment.text_status = TextStatus.DONE if pages else TextStatus.NO_TEXT
        attachment.save(update_fields=["page_count", "text_status", "updated_at"])
    return attachment.text_status


def extract_pending_documents(*, retry_failed: bool = False) -> int:
    """Extrae los manuales pendientes de la organización del contexto; devuelve cuántos procesó."""
    statuses = [TextStatus.PENDING, TextStatus.FAILED] if retry_failed else [TextStatus.PENDING]
    manuals = Attachment.objects.filter(purpose=Purpose.MANUAL, text_status__in=statuses)
    count = 0
    for attachment in manuals.iterator():
        extract_document_text(attachment=attachment)
        count += 1
    return count


def remove_manual(*, attachment: Attachment, by) -> None:
    """Quita un manual: borra el archivo y sus páginas y libera el cupo (auditoría con nombre y huella).

    Los manuales son material de consulta, no historial de trabajo: se borran como
    los adjuntos en borrador (`remove_attachment`).
    """
    if attachment.purpose != Purpose.MANUAL:
        raise AttachmentError(_("Solo se pueden quitar manuales."))
    with transaction.atomic():
        remove_attachment(attachment=attachment, by=by)
