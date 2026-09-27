"""Permisos para quitar y reordenar ítems y quitar fotos del checklist (ADR-013, WO-03).

Gestor: reordena, quita ítems y cualquier foto. Técnico: solo sus propias fotos.
Lector (viewer): nada.
"""

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from apps.accounts.models import Membership, User
from apps.accounts.permissions import PermissionDeniedError
from apps.attachments.models import Attachment
from apps.core.tenancy import tenant_context
from apps.work.services import (
    add_checklist_item,
    add_checklist_photo,
    move_checklist_item,
    remove_checklist_item,
    remove_checklist_photo,
    report_failure,
)
from tests.conftest import PASSWORD, login, tiny_jpeg_bytes


@pytest.fixture(autouse=True)
def _media(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path


def _photo():
    return SimpleUploadedFile("f.jpg", tiny_jpeg_bytes(), content_type="image/jpeg")


def _wo_with_items(tenant):
    with tenant_context(tenant.org):
        wo = report_failure(
            membership=tenant.viewer, asset=tenant.asset, description="Falla", asset_stopped=False
        ).work_order
        first = add_checklist_item(work_order=wo, membership=tenant.admin, text="Revisar")
        second = add_checklist_item(work_order=wo, membership=tenant.admin, text="Limpiar")
    return wo, first, second


@pytest.mark.parametrize("member_name", ["tech", "viewer"])
def test_only_managers_move_or_remove_items(client, tenant_a, member_name):
    wo, _first, second = _wo_with_items(tenant_a)
    member = getattr(tenant_a, member_name)
    with tenant_context(tenant_a.org):
        with pytest.raises(PermissionDeniedError):
            remove_checklist_item(item=second, membership=member)
        with pytest.raises(PermissionDeniedError):
            move_checklist_item(item=second, membership=member, direction="up")
    login(client, member)
    assert (
        client.post(reverse("checklist_item_move", args=[wo.pk, second.pk]), {"direction": "up"}).status_code
        == 403
    )
    assert (
        client.post(reverse("checklist_item_remove", args=[wo.pk, second.pk]), {"confirm": "1"}).status_code
        == 403
    )
    with tenant_context(tenant_a.org):
        assert list(wo.checklist.values_list("text", flat=True)) == ["Revisar", "Limpiar"]


@pytest.mark.parametrize("role", ["planner", "supervisor"])
def test_planner_and_supervisor_move_and_remove_items(tenant_a, role):
    wo, first, second = _wo_with_items(tenant_a)
    member = tenant_a.tech
    member.role = role
    member.save()
    with tenant_context(tenant_a.org):
        move_checklist_item(item=second, membership=member, direction="up")
        remove_checklist_item(item=first, membership=member)
        assert list(wo.checklist.values_list("order", "text")) == [(1, "Limpiar")]


def test_technician_removes_own_photo_but_not_anothers(client, tenant_a):
    wo, first, _second = _wo_with_items(tenant_a)
    other_tech = Membership.objects.create(
        user=User.objects.create_user("otro@empresa-a.cl", PASSWORD, full_name="Otro técnico"),
        organization=tenant_a.org,
        role="technician",
    )
    with tenant_context(tenant_a.org):
        own = add_checklist_photo(item=first, membership=tenant_a.tech, uploaded_file=_photo())
        others = add_checklist_photo(item=first, membership=other_tech, uploaded_file=_photo())
        with pytest.raises(PermissionDeniedError):
            remove_checklist_photo(item=first, photo=others, membership=tenant_a.tech)

    login(client, tenant_a.tech)
    detail = client.get(reverse("work_order_detail", args=[wo.pk])).content.decode()
    own_url = reverse("checklist_photo_delete", args=[wo.pk, first.pk, own.pk])
    others_url = reverse("checklist_photo_delete", args=[wo.pk, first.pk, others.pk])
    assert own_url in detail and others_url not in detail
    assert client.post(others_url).status_code == 403
    assert client.post(own_url).status_code == 302
    with tenant_context(tenant_a.org):
        assert list(Attachment.objects.filter(target_id=first.pk)) == [others]


def test_viewer_cannot_remove_photos(client, tenant_a):
    wo, first, _second = _wo_with_items(tenant_a)
    with tenant_context(tenant_a.org):
        photo = add_checklist_photo(item=first, membership=tenant_a.tech, uploaded_file=_photo())
        with pytest.raises(PermissionDeniedError):
            remove_checklist_photo(item=first, photo=photo, membership=tenant_a.viewer)
    login(client, tenant_a.viewer)
    url = reverse("checklist_photo_delete", args=[wo.pk, first.pk, photo.pk])
    assert client.post(url).status_code == 403
    assert url not in client.get(reverse("work_order_detail", args=[wo.pk])).content.decode()
    with tenant_context(tenant_a.org):
        assert Attachment.objects.filter(pk=photo.pk).exists()
