"""Cupo de almacenamiento de adjuntos (AST-06 CA2, ORG-10 CA1)."""

import io

import pytest
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from PIL import Image

from apps.attachments.models import Attachment, TargetType
from apps.attachments.selectors import BYTES_PER_MB, storage_bytes_used, storage_usage
from apps.attachments.services import AttachmentError, _compress_image, add_attachment
from apps.organizations.models import Plan
from apps.organizations.services import remove_organization_logo, set_organization_logo
from tests.conftest import login
from tests.test_org_logo import image_upload


@pytest.fixture(autouse=True)
def _media(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path


def _set_quota(org, mb):
    org.plan = Plan.objects.create(code=f"p-{mb}", name=f"Plan {mb}", max_storage_mb=mb)
    org.save()


def _fill(tenant, size_bytes):
    """Uso previo simulado: una fila de adjunto con el tamaño pedido (sin archivo real)."""
    return Attachment.objects.create(
        target_type=TargetType.ASSET,
        target_id=tenant.asset.pk,
        file=f"org/{tenant.org.pk}/relleno.bin",
        filename="relleno.bin",
        content_type="application/pdf",
        size_bytes=size_bytes,
    )


def _pdf(size_bytes):
    data = b"%PDF" + b"x" * (size_bytes - 4)
    return SimpleUploadedFile("manual.pdf", data, content_type="application/pdf")


def _upload(tenant, uploaded):
    return add_attachment(
        target_type=TargetType.ASSET, target_id=tenant.asset.pk, uploaded_file=uploaded, by=tenant.admin.user
    )


def test_upload_under_quota_is_accepted(in_a):
    _set_quota(in_a.org, 1)
    _fill(in_a, BYTES_PER_MB // 2)
    attachment = _upload(in_a, _pdf(1000))
    assert attachment.pk is not None
    assert storage_usage(organization=in_a.org).used_bytes == BYTES_PER_MB // 2 + 1000


def test_upload_that_fills_the_quota_exactly_is_accepted(in_a):
    _set_quota(in_a.org, 1)
    _fill(in_a, BYTES_PER_MB - 1000)
    _upload(in_a, _pdf(1000))
    usage = storage_usage(organization=in_a.org)
    assert usage.used_bytes == BYTES_PER_MB
    assert usage.is_full


def test_upload_over_quota_is_rejected_with_a_clear_message(in_a):
    _set_quota(in_a.org, 1)
    _fill(in_a, BYTES_PER_MB - 999)
    with pytest.raises(AttachmentError, match="No hay espacio"):
        _upload(in_a, _pdf(1000))
    assert Attachment.objects.count() == 1


def test_quota_is_measured_after_image_compression(in_a):
    """La foto original no cabe, pero la versión comprimida sí: se acepta y cuenta lo comprimido."""
    buffer = io.BytesIO()
    Image.new("RGB", (3000, 1500), color="blue").save(buffer, format="JPEG", quality=100)
    original = buffer.getvalue()
    compressed, _, _ = _compress_image(io.BytesIO(original))
    assert len(compressed) < len(original)

    _set_quota(in_a.org, 1)
    _fill(in_a, BYTES_PER_MB - len(compressed))
    attachment = _upload(in_a, SimpleUploadedFile("foto.jpg", original, content_type="image/jpeg"))
    assert attachment.size_bytes == len(compressed)
    assert storage_usage(organization=in_a.org).used_bytes == BYTES_PER_MB


def test_compressed_image_one_byte_over_quota_is_rejected(in_a):
    buffer = io.BytesIO()
    Image.new("RGB", (3000, 1500), color="blue").save(buffer, format="JPEG", quality=100)
    original = buffer.getvalue()
    compressed, _, _ = _compress_image(io.BytesIO(original))

    _set_quota(in_a.org, 1)
    _fill(in_a, BYTES_PER_MB - len(compressed) + 1)
    with pytest.raises(AttachmentError, match="No hay espacio"):
        _upload(in_a, SimpleUploadedFile("foto.jpg", original, content_type="image/jpeg"))


def test_plan_without_quota_is_unlimited(in_a):
    _fill(in_a, 50 * BYTES_PER_MB)
    assert _upload(in_a, _pdf(1000)).pk is not None
    usage = storage_usage(organization=in_a.org)
    assert usage.quota_bytes is None
    assert not usage.is_warning and not usage.is_full


def test_warning_starts_at_80_percent(in_a):
    _set_quota(in_a.org, 10)
    filler = _fill(in_a, 8 * BYTES_PER_MB - 1)
    assert not storage_usage(organization=in_a.org).is_warning
    filler.size_bytes = 8 * BYTES_PER_MB
    filler.save()
    usage = storage_usage(organization=in_a.org)
    assert usage.is_warning and not usage.is_full


def test_storage_usage_is_a_single_query(in_a, django_assert_num_queries):
    _set_quota(in_a.org, 10)
    _fill(in_a, 1000)
    with django_assert_num_queries(1):
        storage_usage(organization=in_a.org)


# ---------- Vista ----------


def test_asset_page_shows_usage_and_warning(client, tenant_a):
    from apps.core.tenancy import tenant_context

    _set_quota(tenant_a.org, 10)
    with tenant_context(tenant_a.org):
        _fill(tenant_a, 9 * BYTES_PER_MB)
    login(client, tenant_a.admin)
    page = client.get(reverse("asset_detail", args=[tenant_a.asset.pk])).content.decode()
    assert "9,0 MB de 10 MB usados" in page
    assert "badge--warning" in page


def test_asset_page_disables_upload_when_quota_is_full(client, tenant_a):
    from apps.core.tenancy import tenant_context

    _set_quota(tenant_a.org, 1)
    with tenant_context(tenant_a.org):
        _fill(tenant_a, BYTES_PER_MB)
    login(client, tenant_a.admin)
    page = client.get(reverse("asset_detail", args=[tenant_a.asset.pk])).content.decode()
    assert "Cupo lleno" in page
    assert "badge--error" in page
    assert 'type="submit" disabled' in page


def test_upload_over_quota_via_the_view_shows_an_error(client, tenant_a):
    from apps.core.tenancy import tenant_context

    _set_quota(tenant_a.org, 1)
    with tenant_context(tenant_a.org):
        _fill(tenant_a, BYTES_PER_MB)
    login(client, tenant_a.admin)
    response = client.post(
        reverse("asset_attachment_add", args=[tenant_a.asset.pk]),
        {"purpose": "document", "file": _pdf(1000)},
        follow=True,
    )
    assert "No hay espacio" in response.content.decode()
    with tenant_context(tenant_a.org):
        assert Attachment.objects.count() == 1


# --- El logo de la organización cuenta para el cupo (ORG-10) ---


def _logo_size(org):
    org.refresh_from_db()
    return org.logo_size_bytes


def test_logo_counts_toward_storage_usage(in_a):

    _fill(in_a, 1000)
    set_organization_logo(membership=in_a.admin, uploaded_file=image_upload())
    size = _logo_size(in_a.org)
    assert size == in_a.org.logo.size > 0
    assert storage_usage(organization=in_a.org).used_bytes == 1000 + size
    assert storage_bytes_used(organization=in_a.org) == 1000 + size
    remove_organization_logo(membership=in_a.admin)
    assert _logo_size(in_a.org) == 0
    assert storage_usage(organization=in_a.org).used_bytes == 1000


def test_logo_over_quota_is_rejected_with_a_clear_message(in_a):

    _set_quota(in_a.org, 1)
    _fill(in_a, BYTES_PER_MB - 10)
    with pytest.raises(ValidationError, match="No hay espacio para este logo"):
        set_organization_logo(membership=in_a.admin, uploaded_file=image_upload())
    in_a.org.refresh_from_db()
    assert not in_a.org.logo and in_a.org.logo_size_bytes == 0


def test_replacing_the_logo_only_counts_the_difference(in_a):

    set_organization_logo(membership=in_a.admin, uploaded_file=image_upload())
    size = _logo_size(in_a.org)
    _set_quota(in_a.org, 1)
    _fill(in_a, BYTES_PER_MB - size)  # cupo lleno, logo incluido
    set_organization_logo(membership=in_a.admin, uploaded_file=image_upload())  # mismo tamaño: cabe
    assert _logo_size(in_a.org) == size
    with pytest.raises(AttachmentError, match="No hay espacio"):
        _upload(in_a, _pdf(1000))
