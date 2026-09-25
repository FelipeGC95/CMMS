"""Bandeja de solicitudes y decisiones desde la vista (REQ-01 CA3)."""

import pytest
from django.urls import reverse

from apps.accounts.models import Membership, User
from apps.core.tenancy import tenant_context
from apps.work.models import WorkRequest
from apps.work.services import accept_request, report_failure
from tests.conftest import PASSWORD, login

R = WorkRequest.Status


def test_request_list_shows_only_pending_requests(client, in_a):
    pending = report_failure(
        membership=in_a.viewer, asset=in_a.asset, description="Pendiente", asset_stopped=False
    )
    decided = report_failure(
        membership=in_a.viewer, asset=in_a.asset, description="Decidida", asset_stopped=False
    )
    accept_request(work_request=decided, membership=in_a.admin)
    login(client, in_a.admin)
    shown = {r.pk for r in client.get(reverse("request_list")).context["page"].object_list}
    assert shown == {pending.pk}


def test_accept_view_moves_request_out_of_the_tray(client, in_a):
    request = report_failure(membership=in_a.viewer, asset=in_a.asset, description="x", asset_stopped=False)
    login(client, in_a.admin)
    response = client.post(reverse("request_accept", args=[request.pk]))
    assert response.status_code == 302
    request.refresh_from_db()
    assert request.status == R.ACCEPTED


def test_reject_view_requires_a_reason_and_redirects_with_error(client, in_a):
    request = report_failure(membership=in_a.viewer, asset=in_a.asset, description="x", asset_stopped=False)
    login(client, in_a.admin)
    response = client.post(reverse("request_reject", args=[request.pk]), {"reason": ""}, follow=True)
    request.refresh_from_db()
    assert request.status == R.PENDING
    assert any("motivo" in str(m).lower() for m in response.context["messages"])


def test_merge_view_lists_candidates_and_merges(client, in_a):
    first = report_failure(membership=in_a.viewer, asset=in_a.asset, description="x", asset_stopped=False)
    second = report_failure(membership=in_a.viewer, asset=in_a.asset, description="y", asset_stopped=False)
    login(client, in_a.admin)
    get_response = client.get(reverse("request_merge", args=[second.pk]))
    assert first.pk in {c.pk for c in get_response.context["candidates"]}
    post_response = client.post(reverse("request_merge", args=[second.pk]), {"into": str(first.pk)})
    assert post_response.status_code == 302
    second.refresh_from_db()
    assert second.status == R.MERGED and second.merged_into_id == first.pk


# --- Permisos: quién puede decidir -----------------------------------------------


@pytest.mark.parametrize(
    ("role", "status"),
    [
        ("admin", 200),
        ("planner", 200),
        ("supervisor", 200),
        ("technician", 403),
        ("viewer", 403),
        ("external", 403),
    ],
)
def test_request_list_is_only_for_roles_that_manage_work_orders(client, tenant_a, role, status):
    user = User.objects.create_user(f"{role}-triage@empresa-a.cl", PASSWORD)
    member = Membership.objects.create(user=user, organization=tenant_a.org, role=role)
    login(client, member)
    assert client.get(reverse("request_list")).status_code == status


@pytest.mark.parametrize("role", ["technician", "viewer", "external"])
def test_roles_without_work_order_manage_cannot_decide(client, tenant_a, role):
    with tenant_context(tenant_a.org):
        request = report_failure(
            membership=tenant_a.viewer, asset=tenant_a.asset, description="x", asset_stopped=False
        )
    user = User.objects.create_user(f"{role}-decide@empresa-a.cl", PASSWORD)
    member = Membership.objects.create(user=user, organization=tenant_a.org, role=role)
    login(client, member)
    assert client.post(reverse("request_accept", args=[request.pk])).status_code == 403
    assert client.post(reverse("request_reject", args=[request.pk]), {"reason": "x"}).status_code == 403
    assert client.get(reverse("request_merge", args=[request.pk])).status_code == 403


def test_request_tray_hides_foreign_organization_requests(client, tenant_a, tenant_b):
    with tenant_context(tenant_a.org):
        own = report_failure(
            membership=tenant_a.viewer, asset=tenant_a.asset, description="x", asset_stopped=False
        )
    with tenant_context(tenant_b.org):
        foreign = report_failure(
            membership=tenant_b.viewer, asset=tenant_b.asset, description="x", asset_stopped=False
        )
    login(client, tenant_a.admin)
    page = client.get(reverse("request_list")).context["page"]
    shown = {r.pk for r in page.paginator.object_list}
    assert own.pk in shown
    assert foreign.pk not in shown
    assert client.post(reverse("request_accept", args=[foreign.pk])).status_code == 404
    assert client.post(reverse("request_reject", args=[foreign.pk]), {"reason": "x"}).status_code == 404
    assert client.get(reverse("request_merge", args=[foreign.pk])).status_code == 404
