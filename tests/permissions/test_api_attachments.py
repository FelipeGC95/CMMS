"""API v1 (fotos): solo quien ejecuta OT sube fotos; un token de solo lectura no escribe."""

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

from apps.accounts.permissions import PermissionDeniedError
from apps.api.services import upload_checklist_photo_idempotent, upload_work_order_photo_idempotent
from apps.attachments.models import Attachment
from apps.core.tenancy import tenant_context
from apps.maintenance.models import ItemType
from apps.work.models import WorkOrderChecklistItem
from apps.work.services import add_checklist_photo, add_work_order_photo, create_work_order
from tests.api_utils import api_client
from tests.conftest import tiny_jpeg_bytes

pytestmark = pytest.mark.django_db


def _jpeg():
    return SimpleUploadedFile("f.jpg", tiny_jpeg_bytes(), content_type="image/jpeg")


@pytest.fixture(autouse=True)
def _media(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path


@pytest.fixture
def wo_item(tenant_a):
    with tenant_context(tenant_a.org):
        wo = create_work_order(work_type="preventive", title="A", asset=tenant_a.asset)
        item = WorkOrderChecklistItem.objects.create(work_order=wo, text="Foto", item_type=ItemType.PHOTO)
    return wo, item


def test_read_token_cannot_upload_but_can_list(tenant_a, wo_item):
    wo, item = wo_item
    client = api_client(tenant_a, scope="read")
    for url in (
        f"/api/v1/work-orders/{wo.pk}/photos/",
        f"/api/v1/work-orders/{wo.pk}/checklist-items/{item.pk}/photos/",
    ):
        assert client.post(url, {"file": _jpeg()}, format="multipart").status_code == 403, url
    assert client.get(f"/api/v1/work-orders/{wo.pk}/attachments/").status_code == 200
    assert client.get(f"/api/v1/work-orders/{wo.pk}/checklist-items/").status_code == 200
    with tenant_context(tenant_a.org):
        assert not Attachment.objects.exists()


def test_unauthenticated_upload_is_401(client, wo_item):
    wo, _item = wo_item
    response = client.post(f"/api/v1/work-orders/{wo.pk}/photos/", {"file": _jpeg()})
    assert response.status_code == 401


@pytest.mark.parametrize("role", ["viewer", "external"])
def test_roles_without_execute_cannot_upload_photos(tenant_a, wo_item, role):
    wo, item = wo_item
    member = tenant_a.viewer
    member.role = role
    member.save(update_fields=["role"])
    calls = [
        lambda: add_work_order_photo(work_order=wo, membership=member, uploaded_file=_jpeg()),
        lambda: add_checklist_photo(item=item, membership=member, uploaded_file=_jpeg()),
        lambda: upload_work_order_photo_idempotent(membership=member, work_order=wo, uploaded_file=_jpeg()),
        lambda: upload_checklist_photo_idempotent(membership=member, item=item, uploaded_file=_jpeg()),
    ]
    with tenant_context(tenant_a.org):
        for call in calls:
            with pytest.raises(PermissionDeniedError):
                call()
        assert not Attachment.objects.exists()


@pytest.mark.parametrize("role", ["technician", "supervisor", "planner", "admin"])
def test_roles_that_execute_can_upload_photos(tenant_a, wo_item, role):
    wo, item = wo_item
    member = tenant_a.tech
    member.role = role
    member.save(update_fields=["role"])
    with tenant_context(tenant_a.org):
        add_work_order_photo(work_order=wo, membership=member, uploaded_file=_jpeg())
        add_checklist_photo(item=item, membership=member, uploaded_file=_jpeg())
        assert Attachment.objects.count() == 2
