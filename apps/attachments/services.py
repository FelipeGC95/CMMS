"""Alta de adjuntos: valida tamaño/tipo, comprime imágenes (AST-06 CA1) y respeta el cupo (CA2)."""

import hashlib
import io
from pathlib import Path

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.db import transaction
from django.template.defaultfilters import filesizeformat
from django.utils.translation import gettext as _
from PIL import Image, ImageOps, UnidentifiedImageError

from apps.core.tenancy import require_current_organization
from apps.organizations.models import Organization

from .models import Attachment, Purpose
from .selectors import storage_usage

MAX_IMAGE_DIMENSION = 2000
JPEG_QUALITY = 82
IMAGE_CONTENT_TYPES = {"image/jpeg", "image/png", "image/webp", "image/heic"}
ALLOWED_UPLOAD_TYPES = IMAGE_CONTENT_TYPES | {"application/pdf"}


class AttachmentError(ValidationError):
    pass


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
    if image.mode not in ("RGB", "L"):
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
        raise AttachmentError(
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
