"""API v1 (fotos): un token de A no sube fotos a OT o ítems de B ni ve sus adjuntos."""

import uuid

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

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


@pytest.fixture
def b_photos(tenant_b, settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    with tenant_context(tenant_b.org):
        wo = create_work_order(work_type="preventive", title="B", asset=tenant_b.asset)
        item = WorkOrderChecklistItem.objects.create(work_order=wo, text="Foto", item_type=ItemType.PHOTO)
        wo_photo = add_work_order_photo(
            work_order=wo, membership=tenant_b.admin, uploaded_file=_jpeg(), client_uuid=uuid.uuid4()
        )
        item_photo = add_checklist_photo(item=item, membership=tenant_b.admin, uploaded_file=_jpeg())
    return {
        "wo": wo,
        "item": item,
        "photos": {str(wo_photo.pk), str(item_photo.pk)},
        "key": wo_photo.client_uuid,
    }


def test_cannot_upload_to_other_organization_work_order_or_item(tenant_a, tenant_b, b_photos):
    with tenant_context(tenant_a.org):
        wo_a = create_work_order(work_type="preventive", title="A", asset=tenant_a.asset)
    client = api_client(tenant_a)
    wo_b, item_b = b_photos["wo"], b_photos["item"]
    urls = [
        f"/api/v1/work-orders/{wo_b.pk}/photos/",
        f"/api/v1/work-orders/{wo_b.pk}/checklist-items/{item_b.pk}/photos/",
        # Ni colgando el ítem de B de una OT de A.
        f"/api/v1/work-orders/{wo_a.pk}/checklist-items/{item_b.pk}/photos/",
    ]
    for url in urls:
        assert client.post(url, {"file": _jpeg()}, format="multipart").status_code == 404, url
    with tenant_context(tenant_b.org):
        assert Attachment.objects.count() == 2
    with tenant_context(tenant_a.org):
        assert not Attachment.objects.exists()


def test_cannot_list_other_organization_attachments_or_items(tenant_a, b_photos):
    client = api_client(tenant_a)
    wo_b = b_photos["wo"]
    assert client.get(f"/api/v1/work-orders/{wo_b.pk}/attachments/").status_code == 404
    assert client.get(f"/api/v1/work-orders/{wo_b.pk}/checklist-items/").status_code == 404


def test_attachments_of_own_work_order_never_include_other_organization(tenant_a, b_photos):
    with tenant_context(tenant_a.org):
        wo_a = create_work_order(work_type="preventive", title="A", asset=tenant_a.asset)
    body = api_client(tenant_a).get(f"/api/v1/work-orders/{wo_a.pk}/attachments/").json()
    assert body["count"] == 0 and not ({row["id"] for row in body["results"]} & b_photos["photos"])


def test_client_uuid_of_other_organization_does_not_return_its_photo(tenant_a, b_photos):
    with tenant_context(tenant_a.org):
        wo_a = create_work_order(work_type="corrective", title="A", asset=tenant_a.asset)
    response = api_client(tenant_a).post(
        f"/api/v1/work-orders/{wo_a.pk}/photos/",
        {"file": _jpeg(), "client_uuid": str(b_photos["key"])},
        format="multipart",
    )
    assert response.status_code == 201, response.content
    assert response.json()["id"] not in b_photos["photos"]
    assert response.json()["target_id"] == str(wo_a.pk)
