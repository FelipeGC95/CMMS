"""Ayudas compartidas por los tests de exportación y borrado de organizaciones (X-03, X-04)."""

import csv
import io
import zipfile

from django.core.files.base import ContentFile

from apps.attachments.models import Attachment, Purpose, TargetType
from apps.core.tenancy import tenant_context


def add_attachment(tenant, *, name="manual.pdf", content=b"%PDF-1.4 manual", purpose=Purpose.MANUAL):
    with tenant_context(tenant.org):
        attachment = Attachment(
            target_type=TargetType.ASSET,
            target_id=tenant.asset.pk,
            purpose=purpose,
            filename=name,
            content_type="application/pdf",
            size_bytes=len(content),
        )
        attachment.organization = tenant.org
        attachment.file.save(name, ContentFile(content), save=False)
        attachment.save()
    return attachment


def read_zip(fieldfile) -> dict[str, bytes]:
    with fieldfile.open("rb") as handle, zipfile.ZipFile(io.BytesIO(handle.read())) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


def csv_rows(data: bytes) -> list[dict]:
    assert data.startswith(b"\xef\xbb\xbf")  # BOM para Excel
    return list(csv.DictReader(io.StringIO(data.decode("utf-8-sig"))))
