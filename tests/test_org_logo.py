"""Logo de la organización (ORG-09, parte mínima): validación, ajuste y almacenamiento."""

import io

import pytest
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from PIL import Image

from apps.audit.models import AuditEvent
from apps.organizations.services import (
    LOGO_MAX_BYTES,
    LOGO_MAX_DIMENSION,
    remove_organization_logo,
    set_organization_logo,
)
from tests.conftest import login


@pytest.fixture(autouse=True)
def _media(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path


def image_upload(size=(40, 20), fmt="PNG", mode="RGBA", name="logo.png", content_type="image/png"):
    buffer = io.BytesIO()
    color = (11, 46, 79, 128) if mode == "RGBA" else "navy"
    Image.new(mode, size, color=color).save(buffer, format=fmt)
    return SimpleUploadedFile(name, buffer.getvalue(), content_type=content_type)


def test_logo_is_stored_as_png_under_the_organization_prefix(in_a):
    org = set_organization_logo(membership=in_a.admin, uploaded_file=image_upload())
    assert org.logo.name.startswith(f"org/{in_a.org.pk}/logo-")
    assert org.logo.name.endswith(".png")
    with org.logo.open("rb") as stored:
        image = Image.open(stored)
        assert image.format == "PNG"
        assert image.mode == "RGBA"  # la transparencia se conserva
    assert AuditEvent.objects.filter(changes__has_key="logo").exists()


def test_large_logo_is_resized_keeping_proportions(in_a):
    upload = image_upload(
        size=(1800, 600), fmt="JPEG", mode="RGB", name="logo.jpg", content_type="image/jpeg"
    )
    org = set_organization_logo(membership=in_a.admin, uploaded_file=upload)
    with org.logo.open("rb") as stored:
        assert Image.open(stored).size == (LOGO_MAX_DIMENSION, LOGO_MAX_DIMENSION // 3)


def test_logo_over_one_megabyte_is_rejected(in_a):
    big = SimpleUploadedFile("logo.png", b"x" * (LOGO_MAX_BYTES + 1), content_type="image/png")
    with pytest.raises(ValidationError, match="1 MB"):
        set_organization_logo(membership=in_a.admin, uploaded_file=big)
    in_a.org.refresh_from_db()
    assert not in_a.org.logo


@pytest.mark.parametrize(
    "upload",
    [
        SimpleUploadedFile(
            "logo.svg", b"<svg xmlns='http://www.w3.org/2000/svg'/>", content_type="image/svg+xml"
        ),
        SimpleUploadedFile("logo.png", b"no es una imagen", content_type="image/png"),
    ],
)
def test_non_image_or_unsupported_format_is_rejected(in_a, upload):
    with pytest.raises(ValidationError, match="PNG, JPEG o WebP"):
        set_organization_logo(membership=in_a.admin, uploaded_file=upload)


def test_gif_is_rejected_even_with_an_image_content_type(in_a):
    upload = image_upload(fmt="GIF", mode="RGB", name="logo.gif", content_type="image/gif")
    with pytest.raises(ValidationError, match="PNG, JPEG o WebP"):
        set_organization_logo(membership=in_a.admin, uploaded_file=upload)


def test_replacing_and_removing_the_logo_deletes_the_old_file(in_a, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):
        first = set_organization_logo(membership=in_a.admin, uploaded_file=image_upload()).logo.name
    with django_capture_on_commit_callbacks(execute=True):
        org = set_organization_logo(membership=in_a.admin, uploaded_file=image_upload())
    second = org.logo.name
    storage = org.logo.storage
    assert first != second
    assert not storage.exists(first) and storage.exists(second)
    with django_capture_on_commit_callbacks(execute=True):
        org = remove_organization_logo(membership=in_a.admin)
    assert not org.logo
    assert not storage.exists(second)


def test_admin_uploads_and_sees_the_logo_in_settings(client, tenant_a):
    login(client, tenant_a.admin)
    response = client.post(reverse("organization_settings"), {"section": "logo", "logo": image_upload()})
    assert response.status_code == 302
    body = client.get(reverse("organization_settings")).content.decode()
    assert reverse("organization_logo") in body
    logo = client.get(reverse("organization_logo"))
    assert logo.status_code == 200
    assert logo["Content-Type"] == "image/png"


def test_admin_removes_the_logo_from_settings(client, tenant_a):
    login(client, tenant_a.admin)
    client.post(reverse("organization_settings"), {"section": "logo", "logo": image_upload()})
    client.post(reverse("organization_settings"), {"section": "logo", "remove": "on"})
    tenant_a.org.refresh_from_db()
    assert not tenant_a.org.logo


def test_settings_shows_the_validation_error(client, tenant_a):
    login(client, tenant_a.admin)
    bad = SimpleUploadedFile("logo.png", b"nada", content_type="image/png")
    response = client.post(reverse("organization_settings"), {"section": "logo", "logo": bad})
    assert response.status_code == 200
    assert "PNG, JPEG o WebP" in response.content.decode()


def test_logo_url_is_404_without_logo(client, tenant_a):
    login(client, tenant_a.admin)
    assert client.get(reverse("organization_logo")).status_code == 404
