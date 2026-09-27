"""Permisos del motor preventivo: tipos de ítem, ajustes, absorción y correctiva desde hallazgo."""

import pytest
from django.urls import reverse

from apps.accounts.permissions import PermissionDeniedError
from apps.core.tenancy import tenant_context
from apps.maintenance.models import ItemType
from apps.maintenance.services import create_plan
from apps.work.services import answer_checklist_item, create_work_order
from tests.conftest import login


@pytest.fixture(autouse=True)
def _media(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path


def _text_item(tenant):
    plan = create_plan(
        code="PTX", name="Texto", calendar_interval=30, items=[{"text": "Nota", "item_type": ItemType.TEXT}]
    )
    wo = create_work_order(
        work_type="preventive", title="x", asset=tenant.asset, checklist=list(plan.items.all())
    )
    return wo, wo.checklist.get()


def test_viewer_cannot_answer_a_text_item(in_a):
    _wo, item = _text_item(in_a)
    with pytest.raises(PermissionDeniedError):
        answer_checklist_item(item=item, membership=in_a.viewer, value_text="x")


def test_viewer_gets_403_on_text_answer_view(client, tenant_a):
    with tenant_context(tenant_a.org):
        wo, item = _text_item(tenant_a)
    login(client, tenant_a.viewer)
    url = reverse("checklist_answer", args=[wo.pk, item.pk])
    assert client.post(url, {"value_text": "x"}).status_code == 403


@pytest.mark.parametrize("role", ["planner", "supervisor", "technician", "viewer"])
def test_only_admins_edit_maintenance_settings(client, tenant_a, role):
    from apps.accounts.models import Membership, User
    from apps.maintenance.services import update_maintenance_settings

    user = User.objects.create_user(f"{role}-x@a.cl", "x")
    membership = Membership.objects.create(user=user, organization=tenant_a.org, role=role)
    with tenant_context(tenant_a.org), pytest.raises(PermissionDeniedError):
        update_maintenance_settings(membership=membership, horizon_days=30)
    login(client, membership)
    assert client.get(reverse("maintenance_settings")).status_code == 403
    assert client.post(reverse("maintenance_settings"), {"horizon_days": "30"}).status_code == 403
