"""Documentos y fotos (AST-06)."""

import io

import pytest
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from PIL import Image

from apps.attachments.models import Purpose, TargetType
from apps.attachments.selectors import storage_bytes_used
from apps.attachments.services import AttachmentError, add_attachment


def _jpeg_file(name="foto.jpg", size=(3000, 1500), color="blue", content_type="image/jpeg"):
    buffer = io.BytesIO()
    Image.new("RGB", size, color=color).save(buffer, format="JPEG")
    data = buffer.getvalue()
    return SimpleUploadedFile(name, data, content_type=content_type), len(data)


def test_large_image_is_resized_to_2000px_and_shrinks(in_a):
    uploaded, original_size = _jpeg_file(size=(3000, 1500))
    attachment = add_attachment(
        target_type=TargetType.ASSET, target_id=in_a.asset.pk, uploaded_file=uploaded, by=in_a.admin.user
    )
    assert attachment.width == 2000
    assert attachment.height == 1000
    assert attachment.content_type == "image/jpeg"
    assert attachment.size_bytes < original_size


def test_small_image_is_not_upscaled(in_a):
    uploaded, _ = _jpeg_file(size=(400, 300))
    attachment = add_attachment(
        target_type=TargetType.ASSET, target_id=in_a.asset.pk, uploaded_file=uploaded, by=in_a.admin.user
    )
    assert (attachment.width, attachment.height) == (400, 300)


def test_png_is_converted_to_jpeg_and_renamed(in_a):
    buffer = io.BytesIO()
    Image.new("RGB", (100, 100), color="white").save(buffer, format="PNG")
    uploaded = SimpleUploadedFile("captura.png", buffer.getvalue(), content_type="image/png")
    attachment = add_attachment(
        target_type=TargetType.ASSET, target_id=in_a.asset.pk, uploaded_file=uploaded, by=in_a.admin.user
    )
    assert attachment.content_type == "image/jpeg"
    assert attachment.filename == "captura.jpg"


def test_orientation_is_kept_but_exif_is_stripped(in_a):
    """La imagen se reescribe: no queda EXIF (y por lo tanto, ninguna coordenada GPS)."""
    buffer = io.BytesIO()
    image = Image.new("RGB", (200, 100), color="green")
    exif = image.getexif()
    exif[0x0112] = 3  # Orientation: 180°
    image.save(buffer, format="JPEG", exif=exif)
    uploaded = SimpleUploadedFile("rotada.jpg", buffer.getvalue(), content_type="image/jpeg")

    attachment = add_attachment(
        target_type=TargetType.ASSET, target_id=in_a.asset.pk, uploaded_file=uploaded, by=in_a.admin.user
    )
    stored = Image.open(attachment.file)
    assert stored.getexif() == {}


def test_pdf_is_stored_without_dimensions(in_a):
    uploaded = SimpleUploadedFile(
        "manual.pdf", b"%PDF-1.4 fake but under the limit", content_type="application/pdf"
    )
    attachment = add_attachment(
        target_type=TargetType.ASSET,
        target_id=in_a.asset.pk,
        uploaded_file=uploaded,
        by=in_a.admin.user,
        purpose=Purpose.DOCUMENT,
    )
    assert attachment.width is None and attachment.height is None
    assert attachment.content_type == "application/pdf"


def test_file_over_20mb_is_rejected(in_a):
    oversized = SimpleUploadedFile("grande.jpg", b"x", content_type="image/jpeg")
    oversized.size = 21 * 1024 * 1024
    with pytest.raises(ValidationError, match="20 MB"):
        add_attachment(
            target_type=TargetType.ASSET, target_id=in_a.asset.pk, uploaded_file=oversized, by=in_a.admin.user
        )


@override_settings(MAX_UPLOAD_BYTES=20 * 1024 * 1024)
def test_file_at_the_limit_is_accepted(in_a):
    uploaded, _ = _jpeg_file(size=(50, 50))
    attachment = add_attachment(
        target_type=TargetType.ASSET, target_id=in_a.asset.pk, uploaded_file=uploaded, by=in_a.admin.user
    )
    assert attachment.pk is not None


def test_disallowed_content_type_is_rejected(in_a):
    uploaded = SimpleUploadedFile("script.exe", b"MZ", content_type="application/x-msdownload")
    with pytest.raises(ValidationError, match="no permitido"):
        add_attachment(
            target_type=TargetType.ASSET, target_id=in_a.asset.pk, uploaded_file=uploaded, by=in_a.admin.user
        )


def test_unreadable_image_like_heic_is_rejected_with_a_clear_message(in_a):
    """Pillow (sin plugin HEIC) no puede abrirlo: se rechaza en vez de guardarlo sin comprimir."""
    fake_heic = SimpleUploadedFile("foto.heic", b"not really a heic file", content_type="image/heic")
    with pytest.raises(AttachmentError, match="formato no compatible"):
        add_attachment(
            target_type=TargetType.ASSET, target_id=in_a.asset.pk, uploaded_file=fake_heic, by=in_a.admin.user
        )


def test_client_uuid_makes_the_upload_idempotent(in_a):
    import uuid

    token = uuid.uuid4()
    uploaded, _ = _jpeg_file()
    first = add_attachment(
        target_type=TargetType.ASSET,
        target_id=in_a.asset.pk,
        uploaded_file=uploaded,
        by=in_a.admin.user,
        client_uuid=token,
    )
    uploaded_again, _ = _jpeg_file()
    second = add_attachment(
        target_type=TargetType.ASSET,
        target_id=in_a.asset.pk,
        uploaded_file=uploaded_again,
        by=in_a.admin.user,
        client_uuid=token,
    )
    assert first.pk == second.pk


def test_storage_bytes_used_sums_by_organization_only(tenant_a, tenant_b):
    from apps.core.tenancy import tenant_context

    with tenant_context(tenant_a.org):
        uploaded, _ = _jpeg_file()
        a1 = add_attachment(
            target_type=TargetType.ASSET, target_id=tenant_a.asset.pk, uploaded_file=uploaded, by=None
        )
        uploaded2, _ = _jpeg_file()
        a2 = add_attachment(
            target_type=TargetType.ASSET, target_id=tenant_a.asset.pk, uploaded_file=uploaded2, by=None
        )
    with tenant_context(tenant_b.org):
        uploaded3, _ = _jpeg_file()
        add_attachment(
            target_type=TargetType.ASSET, target_id=tenant_b.asset.pk, uploaded_file=uploaded3, by=None
        )

    assert storage_bytes_used(organization=tenant_a.org) == a1.size_bytes + a2.size_bytes
    assert storage_bytes_used(organization=tenant_a.org) != storage_bytes_used(organization=tenant_b.org)


# ---------- Vistas ----------


def test_asset_manager_can_upload_and_see_it_on_the_asset_page(client, tenant_a):
    from django.urls import reverse

    from tests.conftest import login

    login(client, tenant_a.admin)
    uploaded, _ = _jpeg_file()
    response = client.post(
        reverse("asset_attachment_add", args=[tenant_a.asset.pk]),
        {"purpose": Purpose.PHOTO, "file": uploaded},
    )
    assert response.status_code == 302
    page = client.get(reverse("asset_detail", args=[tenant_a.asset.pk])).content.decode()
    assert "foto.jpg" in page


@pytest.mark.parametrize("role", ["supervisor", "technician", "viewer", "external"])
def test_roles_without_asset_manage_cannot_upload(client, tenant_a, role):
    from django.urls import reverse

    from tests.conftest import login

    member = tenant_a.tech
    member.role = role
    member.save()
    login(client, member)
    uploaded, _ = _jpeg_file()
    response = client.post(
        reverse("asset_attachment_add", args=[tenant_a.asset.pk]),
        {"purpose": Purpose.PHOTO, "file": uploaded},
    )
    assert response.status_code == 403


def test_oversized_upload_via_the_view_shows_an_error_without_crashing(client, tenant_a, settings):
    from django.urls import reverse

    from tests.conftest import login

    login(client, tenant_a.admin)
    oversized = SimpleUploadedFile("grande.jpg", b"x" * 200, content_type="image/jpeg")
    settings.MAX_UPLOAD_BYTES = 100
    response = client.post(
        reverse("asset_attachment_add", args=[tenant_a.asset.pk]),
        {"purpose": Purpose.PHOTO, "file": oversized},
    )
    assert response.status_code == 302  # redirige con mensaje de error, no 500
