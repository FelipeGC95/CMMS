"""Quitar y reordenar ítems y quitar fotos: la organización B no toca el checklist de A."""

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from apps.attachments.models import Attachment
from apps.core.tenancy import tenant_context
from apps.work.services import add_checklist_item, add_checklist_photo, report_failure
from tests.conftest import login, tiny_jpeg_bytes


@pytest.fixture(autouse=True)
def _media(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path


def _wo_with_photo(tenant):
    with tenant_context(tenant.org):
        wo = report_failure(
            membership=tenant.viewer, asset=tenant.asset, description="Falla", asset_stopped=False
        ).work_order
        first = add_checklist_item(work_order=wo, membership=tenant.admin, text="Revisar")
        second = add_checklist_item(work_order=wo, membership=tenant.admin, text="Limpiar")
        photo = add_checklist_photo(
            item=first,
            membership=tenant.tech,
            uploaded_file=SimpleUploadedFile("f.jpg", tiny_jpeg_bytes(), content_type="image/jpeg"),
        )
    return wo, first, second, photo


def test_b_cannot_move_or_remove_items_of_a(client, tenant_a, tenant_b):
    wo, _first, second, _photo = _wo_with_photo(tenant_a)
    login(client, tenant_b.admin)
    assert (
        client.post(reverse("checklist_item_move", args=[wo.pk, second.pk]), {"direction": "up"}).status_code
        == 404
    )
    assert client.post(reverse("checklist_item_remove", args=[wo.pk, second.pk])).status_code == 404
    with tenant_context(tenant_a.org):
        assert list(wo.checklist.values_list("text", flat=True)) == ["Revisar", "Limpiar"]


def test_b_cannot_remove_photos_of_a(client, tenant_a, tenant_b):
    wo, first, _second, photo = _wo_with_photo(tenant_a)
    login(client, tenant_b.admin)
    response = client.post(reverse("checklist_photo_delete", args=[wo.pk, first.pk, photo.pk]))
    assert response.status_code == 404
    assert Attachment.all_tenants.filter(pk=photo.pk).exists()  # all_tenants: verificación global


def test_b_cannot_remove_a_photo_of_a_through_its_own_item(client, tenant_a, tenant_b):
    """La foto se busca dentro del ítem: el id de una foto ajena no sirve con un ítem propio."""
    _wo_a, _first_a, _second_a, photo_a = _wo_with_photo(tenant_a)
    wo_b, first_b, _second_b, _photo_b = _wo_with_photo(tenant_b)
    login(client, tenant_b.admin)
    response = client.post(reverse("checklist_photo_delete", args=[wo_b.pk, first_b.pk, photo_a.pk]))
    assert response.status_code == 404
    assert Attachment.all_tenants.filter(pk=photo_a.pk).exists()  # all_tenants: verificación global
