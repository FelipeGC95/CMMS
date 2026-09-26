"""El cupo de almacenamiento se mide por organización (AST-06 CA2, NF-07)."""

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from apps.attachments.models import Attachment, TargetType
from apps.attachments.selectors import BYTES_PER_MB, storage_usage
from apps.attachments.services import add_attachment
from apps.core.tenancy import tenant_context
from apps.organizations.models import Plan
from tests.conftest import login


@pytest.fixture(autouse=True)
def _media(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path


def _quota(org, mb):
    org.plan = Plan.objects.create(code=f"q-{org.slug}", name=org.slug, max_storage_mb=mb)
    org.save()


def _fill(tenant, size_bytes):
    with tenant_context(tenant.org):
        Attachment.objects.create(
            target_type=TargetType.ASSET,
            target_id=tenant.asset.pk,
            file=f"org/{tenant.org.pk}/relleno.bin",
            filename="relleno.bin",
            content_type="application/pdf",
            size_bytes=size_bytes,
        )


def test_files_of_b_do_not_count_toward_the_quota_of_a(tenant_a, tenant_b):
    _quota(tenant_a.org, 1)
    _fill(tenant_b, 5 * BYTES_PER_MB)  # B supera con creces el cupo de A

    assert storage_usage(organization=tenant_a.org).used_bytes == 0
    with tenant_context(tenant_a.org):
        attachment = add_attachment(
            target_type=TargetType.ASSET,
            target_id=tenant_a.asset.pk,
            uploaded_file=SimpleUploadedFile("m.pdf", b"%PDF-1.4", content_type="application/pdf"),
            by=tenant_a.admin.user,
        )
    assert attachment.pk is not None
    assert storage_usage(organization=tenant_a.org).used_bytes == attachment.size_bytes


def test_asset_page_of_a_shows_only_the_usage_of_a(client, tenant_a, tenant_b):
    _quota(tenant_a.org, 10)
    _fill(tenant_a, 1 * BYTES_PER_MB)
    _fill(tenant_b, 9 * BYTES_PER_MB)
    login(client, tenant_a.admin)
    page = client.get(reverse("asset_detail", args=[tenant_a.asset.pk])).content.decode()
    assert "1,0 MB de 10 MB usados" in page
    assert "badge--warning" not in page
