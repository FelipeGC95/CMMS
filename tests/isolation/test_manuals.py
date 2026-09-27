"""Manuales y su texto por organización (AST-06 CA3, NF-07): B nunca ve, busca ni toca los de A."""

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from apps.attachments import tasks as attachment_tasks
from apps.attachments.models import Attachment, DocumentPage, TextStatus
from apps.attachments.selectors import manuals_for_asset, search_manual_pages, visible_manuals
from apps.core.tenancy import tenant_context
from tests.conftest import login, make_manual, pdf_bytes


@pytest.fixture(autouse=True)
def _media(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path


@pytest.fixture
def manuals_a(tenant_a):
    with tenant_context(tenant_a.org):
        own = make_manual(tenant_a.asset, ["secreto de A turbina"], by=tenant_a.admin.user, name="a.pdf")
        cls = make_manual(
            tenant_a.asset.asset_class, ["clase de A turbina"], by=tenant_a.admin.user, name="clase-a.pdf"
        )
    return own, cls


def test_b_does_not_see_pages_or_manuals_of_a(tenant_a, tenant_b, manuals_a):
    with tenant_context(tenant_b.org):
        assert not DocumentPage.objects.exists()
        assert not Attachment.objects.exists()
        assert not visible_manuals(membership=tenant_b.admin).exists()
        assert list(search_manual_pages(membership=tenant_b.admin, query="turbina")) == []
        # Misma clave de clase ("CMP") en B: el manual de clase de A no aparece.
        assert list(manuals_for_asset(asset=tenant_b.asset)) == []


def test_b_search_view_and_asset_page_show_nothing_of_a(client, tenant_a, tenant_b, manuals_a):
    login(client, tenant_b.admin)
    html = client.get(reverse("manual_search"), {"q": "turbina"}).content.decode()
    assert "a.pdf" not in html and "clase-a.pdf" not in html
    html = client.get(reverse("asset_detail", args=[tenant_b.asset.pk])).content.decode()
    assert "clase-a.pdf" not in html


def test_b_cannot_download_remove_or_upload_on_a(client, tenant_a, tenant_b, manuals_a):
    own, cls = manuals_a
    login(client, tenant_b.admin)
    for manual in (own, cls):
        assert client.get(reverse("attachment", args=[manual.pk])).status_code == 404
        assert client.get(reverse("manual_remove", args=[manual.pk])).status_code == 404
        assert client.post(reverse("manual_remove", args=[manual.pk])).status_code == 404
    upload = SimpleUploadedFile("x.pdf", pdf_bytes(["x"]), content_type="application/pdf")
    assert (
        client.post(reverse("asset_manual_add", args=[tenant_a.asset.pk]), {"file": upload}).status_code
        == 404
    )
    upload = SimpleUploadedFile("x.pdf", pdf_bytes(["x"]), content_type="application/pdf")
    url = reverse("asset_class_manual_add", args=[tenant_a.asset.asset_class_id])
    assert client.post(url, {"file": upload}).status_code == 404
    with tenant_context(tenant_a.org):
        assert Attachment.objects.count() == 2


def test_task_with_other_organization_does_not_touch_the_manual(tenant_a, tenant_b):
    from apps.attachments import services

    with tenant_context(tenant_a.org):
        manual = services.add_manual(
            target_type="asset",
            target_id=tenant_a.asset.pk,
            uploaded_file=SimpleUploadedFile("m.pdf", pdf_bytes(["x"]), content_type="application/pdf"),
            by=tenant_a.admin.user,
        )
    result = attachment_tasks.extract_document_text(
        organization_id=str(tenant_b.org.pk), attachment_id=str(manual.pk)
    )
    assert result == ""
    with tenant_context(tenant_a.org):
        manual.refresh_from_db()
        assert manual.text_status == TextStatus.PENDING
