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


@pytest.mark.parametrize("role,status", [("technician", 403), ("supervisor", 403), ("planner", 302)])
def test_only_plan_managers_configure_absorption(client, tenant_a, role, status):
    from apps.accounts.models import Membership, User
    from apps.maintenance.models import PlanAbsorption

    with tenant_context(tenant_a.org):
        minor = create_plan(
            code="MIN", name="Menor", calendar_interval=7, items=[{"text": "x", "item_type": "check"}]
        )
    user = User.objects.create_user(f"{role}-abs@a.cl", "x")
    login(client, Membership.objects.create(user=user, organization=tenant_a.org, role=role))
    url = reverse("plan_absorption_add", args=[tenant_a.plan.pk])
    assert client.post(url, {"absorbed_plan": minor.pk}).status_code == status
    with tenant_context(tenant_a.org):
        assert PlanAbsorption.objects.exists() == (status == 302)


def test_technician_cannot_add_absorption_through_the_service(in_a):
    from apps.maintenance.services import add_plan_absorption

    minor = create_plan(
        code="MIN", name="Menor", calendar_interval=7, items=[{"text": "x", "item_type": "check"}]
    )
    with pytest.raises(PermissionDeniedError):
        add_plan_absorption(membership=in_a.tech, plan=in_a.plan, absorbed_plan=minor)


def test_technician_goes_through_the_failure_report_and_manager_creates_directly(client, tenant_a):
    from apps.work.models import WorkOrder, WorkRequest
    from apps.work.services import answer_checklist_item

    with tenant_context(tenant_a.org):
        wo = create_work_order(
            work_type="preventive", title="x", asset=tenant_a.asset, checklist=list(tenant_a.plan.items.all())
        )
        first, second = wo.checklist.all()
        answer_checklist_item(item=first, membership=tenant_a.tech, result="not_ok")
        answer_checklist_item(item=second, membership=tenant_a.tech, value_number="1")
    login(client, tenant_a.tech)
    page = client.get(reverse("work_order_detail", args=[wo.pk])).content.decode()
    assert "Avisar falla" in page and "Crear correctiva" not in page
    client.post(reverse("checklist_item_corrective", args=[wo.pk, first.pk]), {})
    login(client, tenant_a.admin)
    client.post(reverse("checklist_item_corrective", args=[wo.pk, second.pk]), {})
    with tenant_context(tenant_a.org):
        by_tech = WorkOrder.objects.get(source_checklist_item=first)
        by_admin = WorkOrder.objects.get(source_checklist_item=second)
        assert WorkRequest.objects.filter(work_order=by_tech).exists() and by_tech.status == "requested"
        assert not WorkRequest.objects.filter(work_order=by_admin).exists() and by_admin.status == "planned"
