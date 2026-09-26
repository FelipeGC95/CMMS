"""Checklist de OT: quitar y reordenar ítems y quitar fotos por ítem (WO-03)."""

import pytest
from django.core.files.storage import default_storage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from apps.attachments.models import Attachment
from apps.attachments.selectors import storage_usage
from apps.audit.models import AuditEvent
from apps.core.tenancy import tenant_context
from apps.work.models import WorkOrder
from apps.work.services import (
    WorkOrderError,
    add_checklist_item,
    add_checklist_photo,
    answer_checklist_item,
    create_work_order,
    move_checklist_item,
    remove_checklist_item,
    remove_checklist_photo,
    report_failure,
    save_checklist_note,
)
from tests.conftest import login, tiny_jpeg_bytes


@pytest.fixture(autouse=True)
def _media(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path


def _photo():
    return SimpleUploadedFile("f.jpg", tiny_jpeg_bytes(), content_type="image/jpeg")


def _wo_with_items(tenant, *texts):
    wo = report_failure(
        membership=tenant.viewer, asset=tenant.asset, description="Falla", asset_stopped=False
    ).work_order
    items = [add_checklist_item(work_order=wo, membership=tenant.admin, text=t) for t in texts]
    return wo, items


def _texts(wo):
    return list(wo.checklist.values_list("order", "text"))


# --- Quitar ítems ----------------------------------------------------------------------


def test_remove_unanswered_item_renumbers_and_audits(in_a):
    wo, (first, second, third) = _wo_with_items(in_a, "A", "B", "C")

    remove_checklist_item(item=second, membership=in_a.admin)

    assert _texts(wo) == [(1, "A"), (2, "C")]
    event = AuditEvent.objects.get(target_id=wo.pk, changes__has_key="checklist_item_removed")
    assert event.changes["checklist_item_removed"] == ["B", None]


@pytest.mark.parametrize("evidence", ["result", "note", "photo"])
def test_item_with_answer_note_or_photo_cannot_be_removed(in_a, evidence):
    wo, (item,) = _wo_with_items(in_a, "A")
    if evidence == "result":
        answer_checklist_item(item=item, membership=in_a.tech, result="ok")
    elif evidence == "note":
        save_checklist_note(item=item, membership=in_a.tech, note="Suena raro")
    else:
        add_checklist_photo(item=item, membership=in_a.tech, uploaded_file=_photo())
    item.work_order.refresh_from_db()

    with pytest.raises(WorkOrderError, match="ya tiene respuesta, observación o fotos"):
        remove_checklist_item(item=item, membership=in_a.admin)
    assert wo.checklist.count() == 1


@pytest.mark.parametrize("status", ["completed", "closed", "cancelled"])
def test_items_cannot_be_removed_or_moved_once_the_work_order_is_done(in_a, status):
    wo, (first, second) = _wo_with_items(in_a, "A", "B")
    WorkOrder.objects.filter(pk=wo.pk).update(status=status)
    second.work_order.refresh_from_db()
    with pytest.raises(WorkOrderError, match="no admite cambios"):
        remove_checklist_item(item=second, membership=in_a.admin)
    with pytest.raises(WorkOrderError, match="no admite cambios"):
        move_checklist_item(item=second, membership=in_a.admin, direction="up")
    assert _texts(wo) == [(1, "A"), (2, "B")]


def test_preventive_checklist_keeps_its_plan_snapshot(in_a):
    """Igual que al agregar: el checklist de una preventiva viene de su plan (ADR-016)."""
    wo = create_work_order(
        work_type="preventive", title="PM", asset=in_a.asset, checklist=in_a.plan.items.all()
    )
    purge, pressure = wo.checklist.all()
    with pytest.raises(WorkOrderError, match="preventiva"):
        remove_checklist_item(item=purge, membership=in_a.admin)
    with pytest.raises(WorkOrderError, match="preventiva"):
        move_checklist_item(item=pressure, membership=in_a.admin, direction="up")
    assert _texts(wo) == [(1, "Purgar"), (2, "Presión")]


# --- Reordenar ---------------------------------------------------------------------------


def test_move_up_and_down_swaps_neighbours(in_a):
    wo, (a, b, c) = _wo_with_items(in_a, "A", "B", "C")

    move_checklist_item(item=c, membership=in_a.admin, direction="up")
    assert _texts(wo) == [(1, "A"), (2, "C"), (3, "B")]

    move_checklist_item(item=a, membership=in_a.admin, direction="down")
    assert _texts(wo) == [(1, "C"), (2, "A"), (3, "B")]
    assert AuditEvent.objects.filter(target_id=wo.pk, changes__has_key="checklist_item_moved").count() == 2


def test_moving_past_the_ends_does_nothing(in_a):
    wo, (a, b) = _wo_with_items(in_a, "A", "B")
    move_checklist_item(item=a, membership=in_a.admin, direction="up")
    move_checklist_item(item=b, membership=in_a.admin, direction="down")
    assert _texts(wo) == [(1, "A"), (2, "B")]


def test_answered_items_can_still_be_moved(in_a):
    wo, (a, b) = _wo_with_items(in_a, "A", "B")
    answer_checklist_item(item=b, membership=in_a.tech, result="ok")
    move_checklist_item(item=b, membership=in_a.admin, direction="up")
    assert _texts(wo) == [(1, "B"), (2, "A")]


def test_invalid_direction_is_rejected(in_a):
    _wo, (a,) = _wo_with_items(in_a, "A")
    with pytest.raises(WorkOrderError, match="inválido"):
        move_checklist_item(item=a, membership=in_a.admin, direction="left")


def test_manager_reorders_and_removes_from_the_page_with_htmx(client, tenant_a):
    with tenant_context(tenant_a.org):
        wo, (a, b, c) = _wo_with_items(tenant_a, "A", "B", "C")
        answer_checklist_item(item=a, membership=tenant_a.tech, result="ok")
    login(client, tenant_a.admin)

    page = client.get(reverse("work_order_checklist", args=[wo.pk])).content.decode()
    # El ítem respondido no ofrece «Quitar»; los demás sí.
    assert reverse("checklist_item_remove", args=[wo.pk, a.pk]) not in page
    assert reverse("checklist_item_remove", args=[wo.pk, b.pk]) in page
    assert "Con respuesta" in page

    response = client.post(
        reverse("checklist_item_move", args=[wo.pk, c.pk]), {"direction": "up"}, HTTP_HX_REQUEST="true"
    )
    assert response.status_code == 200
    assert 'id="current-items"' in response.content.decode()
    response = client.post(reverse("checklist_item_remove", args=[wo.pk, b.pk]), HTTP_HX_REQUEST="true")
    assert "Ítem «B» quitado." in response.content.decode()
    response = client.post(reverse("checklist_item_remove", args=[wo.pk, a.pk]), HTTP_HX_REQUEST="true")
    assert "alert--error" in response.content.decode()
    with tenant_context(tenant_a.org):
        assert _texts(wo) == [(1, "A"), (2, "C")]


def test_remove_without_javascript_redirects_with_a_message(client, tenant_a):
    with tenant_context(tenant_a.org):
        wo, (a,) = _wo_with_items(tenant_a, "A")
    login(client, tenant_a.admin)
    response = client.post(reverse("checklist_item_remove", args=[wo.pk, a.pk]), follow=True)
    assert "Ítem «A» quitado." in response.content.decode()
    with tenant_context(tenant_a.org):
        assert not wo.checklist.exists()


# --- Quitar fotos ------------------------------------------------------------------------


def test_uploader_removes_photo_file_and_frees_quota(in_a, django_capture_on_commit_callbacks):
    _wo, (item,) = _wo_with_items(in_a, "Correa")
    photo = add_checklist_photo(item=item, membership=in_a.tech, uploaded_file=_photo())
    name, photo_pk, size = photo.file.name, photo.pk, photo.size_bytes
    used = storage_usage(organization=in_a.org).used_bytes
    assert default_storage.exists(name)

    with django_capture_on_commit_callbacks(execute=True):
        remove_checklist_photo(item=item, photo=photo, membership=in_a.tech)

    assert not Attachment.objects.filter(pk=photo_pk).exists()
    assert not default_storage.exists(name)
    assert storage_usage(organization=in_a.org).used_bytes == used - size
    event = AuditEvent.objects.get(target_id=str(photo_pk), action=AuditEvent.Action.DELETE)
    assert event.changes["sha256"] == [photo.sha256, None]


def test_manager_removes_a_technicians_photo(in_a):
    _wo, (item,) = _wo_with_items(in_a, "Correa")
    photo = add_checklist_photo(item=item, membership=in_a.tech, uploaded_file=_photo())
    photo_pk = photo.pk
    remove_checklist_photo(item=item, photo=photo, membership=in_a.admin)
    assert not Attachment.objects.filter(pk=photo_pk).exists()
    assert Attachment.objects.filter(target_id=item.pk).count() == 0


def test_photos_are_kept_once_the_work_order_is_completed(in_a):
    wo, (item,) = _wo_with_items(in_a, "Correa")
    photo = add_checklist_photo(item=item, membership=in_a.tech, uploaded_file=_photo())
    WorkOrder.objects.filter(pk=wo.pk).update(status="completed")
    item.work_order.refresh_from_db()
    with pytest.raises(WorkOrderError, match="no admite cambios"):
        remove_checklist_photo(item=item, photo=photo, membership=in_a.admin)
    assert Attachment.objects.filter(pk=photo.pk).exists()


def test_photo_of_another_item_is_rejected(in_a):
    _wo, (item, other) = _wo_with_items(in_a, "Correa", "Polea")
    photo = add_checklist_photo(item=item, membership=in_a.tech, uploaded_file=_photo())
    with pytest.raises(WorkOrderError, match="no es de este ítem"):
        remove_checklist_photo(item=other, photo=photo, membership=in_a.admin)
    assert Attachment.objects.filter(pk=photo.pk).exists()


def test_technician_removes_own_photo_from_the_mobile_checklist(client, tenant_a):
    with tenant_context(tenant_a.org):
        wo, (item,) = _wo_with_items(tenant_a, "Correa")
        photo = add_checklist_photo(item=item, membership=tenant_a.tech, uploaded_file=_photo())
    login(client, tenant_a.tech)
    url = reverse("checklist_photo_delete", args=[wo.pk, item.pk, photo.pk])
    detail = client.get(reverse("work_order_detail", args=[wo.pk])).content.decode()
    assert url in detail

    response = client.post(url, HTTP_HX_REQUEST="true")

    assert response.status_code == 200
    html = response.content.decode()
    assert f'id="item-{item.pk}"' in html and url not in html
    with tenant_context(tenant_a.org):
        assert not Attachment.objects.filter(pk=photo.pk).exists()
