"""Cabecera de la OT con la acción principal del estado y panel de filtros de la lista."""

import pytest
from django.urls import reverse

from apps.core.tenancy import tenant_context
from apps.work.services import add_checklist_item, answer_checklist_item, create_work_order, transition
from tests.conftest import login


@pytest.fixture
def wo(tenant_a):
    with tenant_context(tenant_a.org):
        work_order = create_work_order(
            work_type="corrective",
            title="Fuga de aire",
            asset=tenant_a.asset,
            status="planned",
            assigned_to=tenant_a.tech.user,
        )
        add_checklist_item(work_order=work_order, membership=tenant_a.admin, text="Revisar sello")
    return work_order


def _actions(response):
    html = response.content.decode()
    start = html.index('id="wo-actions"')
    return html[start : html.index("</div>", start)]


def test_planned_work_order_offers_start_as_primary(client, tenant_a, wo):
    login(client, tenant_a.tech)
    actions = _actions(client.get(reverse("work_order_detail", args=[wo.pk])))
    assert 'page-actions__primary" form="wo-transition" name="to" value="in_progress"' in actions


def test_in_progress_with_pending_items_points_to_the_checklist(client, tenant_a, wo):
    with tenant_context(tenant_a.org):
        transition(work_order=wo, to_status="in_progress", membership=tenant_a.tech)
    login(client, tenant_a.tech)
    actions = _actions(client.get(reverse("work_order_detail", args=[wo.pk])))
    assert 'page-actions__primary" href="#cl-title"' in actions
    assert 'value="on_hold"' in actions and "btn--secondary" in actions


def test_in_progress_without_pending_items_points_to_complete(client, tenant_a, wo):
    with tenant_context(tenant_a.org):
        answer_checklist_item(item=wo.checklist.get(), membership=tenant_a.tech, result="ok")
    login(client, tenant_a.tech)
    actions = _actions(client.get(reverse("work_order_detail", args=[wo.pk])))
    assert 'page-actions__primary" href="#complete-section"' in actions


def test_on_hold_offers_resume_as_primary(client, tenant_a, wo):
    with tenant_context(tenant_a.org):
        transition(work_order=wo, to_status="in_progress", membership=tenant_a.tech)
        transition(work_order=wo, to_status="on_hold", membership=tenant_a.tech)
    login(client, tenant_a.tech)
    actions = _actions(client.get(reverse("work_order_detail", args=[wo.pk])))
    assert 'page-actions__primary" form="wo-transition" name="to" value="in_progress"' in actions
    assert "Reanudar" in actions


def test_viewer_sees_no_actions(client, tenant_a, wo):
    login(client, tenant_a.viewer)
    response = client.get(reverse("work_order_detail", args=[wo.pk]))
    assert 'id="wo-actions"' not in response.content.decode()


def test_filters_are_collapsed_without_active_filters(client, tenant_a, wo):
    login(client, tenant_a.admin)
    response = client.get(reverse("work_order_list"))
    assert response.context["active_filters"] == 0
    assert '<details class="card mb-4 filters">' in response.content.decode()


def test_filters_open_and_count_active_filters(client, tenant_a, wo):
    login(client, tenant_a.admin)
    response = client.get(
        reverse("work_order_list"), {"status": "planned", "asset_q": "CMP", "date_field": "due_on"}
    )
    assert response.context["active_filters"] == 2
    assert '<details class="card mb-4 filters" open>' in response.content.decode()
