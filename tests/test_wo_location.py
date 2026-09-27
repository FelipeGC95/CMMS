"""OT y solicitudes sobre una ubicación y responsables de apoyo (WO-01 CA2, REQ-01 CA1, WO-02 CA5)."""

import pytest
from django.db import IntegrityError, transaction
from django.urls import reverse

from apps.accounts.models import Membership, User
from apps.assets.models import Asset
from apps.audit.models import AuditEvent
from apps.core.tenancy import tenant_context
from apps.organizations.models import Location
from apps.work.models import FailureEvent, WorkOrder, WorkOrderHelper, WorkRequest
from apps.work.selectors import checklist_plan_versions, work_order_list
from apps.work.services import (
    WorkOrderError,
    accept_request,
    complete_work_order,
    create_work_order,
    merge_request,
    report_failure,
    set_assignees,
    transition,
    work_order_meter,
)
from tests.conftest import PASSWORD, login


@pytest.fixture
def area(in_a):
    site = Location.objects.create(code="P1", name="Planta 1")
    return Location.objects.create(code="P1-BOD", name="Bodega", kind="area", parent=site)


def _member(tenant, role, email):
    user = User.objects.create_user(email, PASSWORD, full_name=email.split("@")[0])
    return Membership.objects.create(user=user, organization=tenant.org, role=role)


# ---------- Servicio: objetivo activo o ubicación ----------


def test_work_order_on_a_location_shares_the_type_sequence(in_a, area):
    on_asset = create_work_order(work_type="corrective", title="A", asset=in_a.asset)
    on_location = create_work_order(work_type="corrective", title="Gotera", location=area)
    assert on_location.asset is None and on_location.location == area
    assert on_location.number == on_asset.number + 1
    assert on_location.code == f"OT-C-{on_location.number:05d}"
    assert on_location.target == area and on_asset.target == in_a.asset


@pytest.mark.parametrize("target", ["both", "none"])
def test_exactly_one_target_is_required(in_a, area, target):
    kwargs = {"asset": in_a.asset, "location": area} if target == "both" else {}
    with pytest.raises(WorkOrderError):
        create_work_order(work_type="corrective", title="x", **kwargs)
    assert not WorkOrder.objects.exists()


@pytest.mark.parametrize("target", ["both", "none"])
def test_database_rejects_a_work_order_without_exactly_one_target(in_a, area, target):
    kwargs = {"asset": in_a.asset, "location": area} if target == "both" else {}
    with pytest.raises(IntegrityError), transaction.atomic():
        WorkOrder.objects.create(number=99, work_type="corrective", status="planned", title="x", **kwargs)


def test_database_rejects_a_stopped_request_without_asset(in_a, area):
    with pytest.raises(IntegrityError), transaction.atomic():
        WorkRequest.objects.create(number=1, location=area, description="x", asset_stopped=True)


def test_corrective_on_a_location_runs_its_lifecycle_without_touching_assets(in_a, area):
    in_a.org.require_failure_details = True  # los eventos de falla son por activo: no aplica aquí
    in_a.org.save()
    wo = create_work_order(work_type="corrective", title="Gotera", location=area)
    assert work_order_meter(wo) is None
    transition(work_order=wo, to_status="in_progress", membership=in_a.tech)
    complete_work_order(work_order=wo, membership=in_a.tech, labor_minutes=30, notes="Sellado")
    wo.refresh_from_db()
    assert wo.status == WorkOrder.Status.CLOSED
    assert wo.labor_cost >= 0 and wo.completion_notes == "Sellado"
    assert not FailureEvent.objects.exists()
    in_a.asset.refresh_from_db()
    assert in_a.asset.status == Asset.Status.OPERATIONAL


def test_location_work_order_lists_all_published_plans_without_suggestions(in_a, area):
    wo = create_work_order(work_type="inspection", title="Ronda", location=area)
    versions = list(checklist_plan_versions(work_order=wo))
    assert [v.plan.code for v in versions] == ["PM1"]
    assert not versions[0].suggested


# ---------- Servicio: responsables de apoyo ----------


def test_helpers_are_saved_without_repeating_the_lead(in_a, area):
    helper = _member(in_a, "technician", "apoyo@empresa-a.cl").user
    wo = create_work_order(
        work_type="corrective",
        title="x",
        location=area,
        assigned_to=in_a.tech.user,
        helpers=[in_a.tech.user, helper],
    )
    assert list(wo.helpers.all()) == [helper]


def test_set_assignees_replaces_helpers_and_is_audited(in_a, area):
    first = _member(in_a, "technician", "uno@empresa-a.cl").user
    second = _member(in_a, "technician", "dos@empresa-a.cl").user
    wo = create_work_order(work_type="corrective", title="x", location=area, helpers=[first])
    set_assignees(work_order=wo, membership=in_a.admin, assigned_to=in_a.tech.user, helpers=[second])
    wo.refresh_from_db()
    assert wo.assigned_to == in_a.tech.user
    assert list(wo.helpers.all()) == [second]
    event = AuditEvent.objects.get(target_id=str(wo.pk), action=AuditEvent.Action.ASSIGN)
    assert event.changes["helpers"] == [[str(first)], [str(second)]]
    assert event.changes["assigned_to"] == ["", str(in_a.tech.user)]


def test_set_assignees_is_rejected_on_a_closed_work_order(in_a, area):
    wo = create_work_order(work_type="corrective", title="x", location=area)
    transition(work_order=wo, to_status="cancelled", membership=in_a.admin)
    with pytest.raises(WorkOrderError):
        set_assignees(work_order=wo, membership=in_a.admin, assigned_to=in_a.tech.user)


def test_my_work_orders_include_the_ones_where_i_help(in_a, area):
    helper = _member(in_a, "technician", "apoyo@empresa-a.cl")
    led = create_work_order(work_type="corrective", title="lidera", asset=in_a.asset, assigned_to=helper.user)
    helped = create_work_order(
        work_type="corrective",
        title="apoya",
        location=area,
        assigned_to=in_a.tech.user,
        helpers=[helper.user],
    )
    other = create_work_order(work_type="corrective", title="otra", location=area)
    mine = set(work_order_list(membership=helper, assigned_to=helper.user).values_list("pk", flat=True))
    assert mine == {led.pk, helped.pk}
    assert other.pk not in mine
    # Sin duplicados aunque la persona aparezca como principal y de apoyo en distintas OT.
    assert work_order_list(membership=helper, assigned_to=helper.user).count() == 2


# ---------- Servicio: solicitudes sobre una ubicación (REQ-01 CA1) ----------


def test_request_on_a_location_creates_its_corrective_on_the_same_location(in_a, area):
    request = report_failure(membership=in_a.viewer, location=area, description="Gotera en bodega")
    assert request.location == area and request.asset is None and not request.asset_stopped
    wo = request.work_order
    assert wo.location == area and wo.asset is None
    assert wo.work_type == WorkOrder.Type.CORRECTIVE and wo.priority == WorkOrder.Priority.NORMAL
    accept_request(work_request=request, membership=in_a.admin)
    request.refresh_from_db()
    assert request.status == WorkRequest.Status.ACCEPTED and request.work_order.location == area


def test_asset_stopped_only_applies_to_an_asset(in_a, area):
    with pytest.raises(WorkOrderError):
        report_failure(membership=in_a.viewer, location=area, description="x", asset_stopped=True)
    assert not WorkRequest.objects.exists()


def test_requests_merge_only_with_the_same_target(in_a, area):
    first = report_failure(membership=in_a.viewer, location=area, description="a")
    second = report_failure(membership=in_a.viewer, location=area, description="b")
    on_asset = report_failure(membership=in_a.viewer, asset=in_a.asset, description="c")
    with pytest.raises(WorkOrderError):
        merge_request(work_request=on_asset, into=first, membership=in_a.admin)
    merge_request(work_request=second, into=first, membership=in_a.admin)
    second.refresh_from_db()
    assert second.status == WorkRequest.Status.MERGED


# ---------- Vistas ----------


def test_create_view_creates_a_location_work_order_with_helpers(client, tenant_a):
    with tenant_context(tenant_a.org):
        area = Location.objects.create(code="BOD", name="Bodega")
    login(client, tenant_a.admin)
    page = client.get(reverse("work_order_create")).content.decode()
    assert "Bodega (BOD)" in page and 'type="checkbox"' in page
    response = client.post(
        reverse("work_order_create"),
        {
            "work_type": "improvement",
            "location": str(area.pk),
            "title": "Pintar demarcación",
            "priority": "3",
            "assigned_to": str(tenant_a.tech.user_id),
            "helpers": [str(tenant_a.admin.user_id)],
        },
    )
    assert response.status_code == 302
    with tenant_context(tenant_a.org):
        wo = WorkOrder.objects.get()
        assert wo.location == area and wo.asset is None
        assert list(wo.helpers.all()) == [tenant_a.admin.user]
    detail = client.get(response["Location"]).content.decode()
    assert "Bodega" in detail and "OT sobre una ubicación" in detail and "Apoyo" in detail


@pytest.mark.parametrize("target", ["both", "none"])
def test_create_view_requires_exactly_one_target(client, tenant_a, target):
    with tenant_context(tenant_a.org):
        area = Location.objects.create(code="BOD", name="Bodega")
    data = {"work_type": "corrective", "title": "x", "priority": "3"}
    if target == "both":
        data.update(asset=str(tenant_a.asset.pk), location=str(area.pk))
    login(client, tenant_a.admin)
    response = client.post(reverse("work_order_create"), data)
    assert response.status_code == 200 and response.context["form"].errors
    with tenant_context(tenant_a.org):
        assert not WorkOrder.objects.exists()


def test_create_view_preloads_the_location_from_the_querystring(client, tenant_a):
    with tenant_context(tenant_a.org):
        area = Location.objects.create(code="BOD", name="Bodega")
    login(client, tenant_a.admin)
    response = client.get(reverse("work_order_create"), {"location": str(area.pk), "work_type": "corrective"})
    assert str(response.context["form"]["location"].value()) == str(area.pk)


def test_location_report_view_creates_the_request_and_its_work_order(client, tenant_a):
    with tenant_context(tenant_a.org):
        area = Location.objects.create(code="BOD", name="Bodega")
    login(client, tenant_a.viewer)
    page = client.get(reverse("location_report")).content.decode()
    assert "Bodega (BOD)" in page and "asset_stopped" not in page
    response = client.post(reverse("location_report"), {"location": str(area.pk), "description": "Gotera"})
    assert response.status_code == 302
    with tenant_context(tenant_a.org):
        request = WorkRequest.objects.get()
        assert request.location == area and request.work_order.location == area
        assert response["Location"] == reverse("work_order_detail", args=[request.work_order_id])


def test_location_report_is_idempotent_by_client_uuid(client, tenant_a):
    with tenant_context(tenant_a.org):
        area = Location.objects.create(code="BOD", name="Bodega")
    login(client, tenant_a.viewer)
    data = {
        "location": str(area.pk),
        "description": "Gotera",
        "client_uuid": "0190f1a4-0000-7000-8000-000000000001",
    }
    client.post(reverse("location_report"), data)
    client.post(reverse("location_report"), data)
    with tenant_context(tenant_a.org):
        assert WorkRequest.objects.count() == 1


def test_list_and_request_inbox_show_the_location(client, tenant_a):
    with tenant_context(tenant_a.org):
        area = Location.objects.create(code="BOD", name="Bodega")
        report_failure(membership=tenant_a.viewer, location=area, description="Gotera")
    login(client, tenant_a.admin)
    assert "Bodega" in client.get(reverse("work_order_list")).content.decode()
    inbox = client.get(reverse("request_list")).content.decode()
    assert "Bodega" in inbox and 'data-label="Activo o ubicación"' in inbox


def test_list_search_matches_the_location(client, tenant_a):
    with tenant_context(tenant_a.org):
        area = Location.objects.create(code="BOD", name="Bodega")
        wo = create_work_order(work_type="corrective", title="x", location=area)
        create_work_order(work_type="corrective", title="y", asset=tenant_a.asset)
    login(client, tenant_a.admin)
    listed = client.get(reverse("work_order_list"), {"scope": "open", "asset_q": "bodega"}).context["page"]
    assert [w.pk for w in listed.object_list] == [wo.pk]


def test_site_filter_includes_location_work_orders_below_the_site(client, tenant_a):
    with tenant_context(tenant_a.org):
        site = Location.objects.create(code="P1", name="Planta 1")
        area = Location.objects.create(code="P1-A", name="Área", kind="area", parent=site)
        other = Location.objects.create(code="P2", name="Planta 2")
        inside = create_work_order(work_type="corrective", title="x", location=area)
        create_work_order(work_type="corrective", title="y", location=other)
    login(client, tenant_a.admin)
    listed = client.get(reverse("work_order_list"), {"scope": "open", "location": site.pk}).context["page"]
    assert [w.pk for w in listed.object_list] == [inside.pk]


def test_helper_sees_the_work_order_in_mis_ot(client, tenant_a):
    with tenant_context(tenant_a.org):
        helper = _member(tenant_a, "technician", "apoyo@empresa-a.cl")
        area = Location.objects.create(code="BOD", name="Bodega")
        wo = create_work_order(
            work_type="corrective",
            title="x",
            location=area,
            assigned_to=tenant_a.tech.user,
            helpers=[helper.user],
        )
    login(client, helper)
    listed = client.get(reverse("work_order_list"), {"scope": "mine"}).context["page"]
    assert [w.pk for w in listed.object_list] == [wo.pk]


def test_assignees_view_updates_lead_and_helpers(client, tenant_a):
    with tenant_context(tenant_a.org):
        wo = create_work_order(work_type="corrective", title="x", asset=tenant_a.asset)
    login(client, tenant_a.admin)
    response = client.post(
        reverse("work_order_assignees", args=[wo.pk]),
        {"assigned_to": str(tenant_a.tech.user_id), "helpers": [str(tenant_a.admin.user_id)]},
    )
    assert response.status_code == 302
    with tenant_context(tenant_a.org):
        wo.refresh_from_db()
        assert wo.assigned_to == tenant_a.tech.user
        assert list(WorkOrderHelper.objects.values_list("user_id", flat=True)) == [tenant_a.admin.user_id]


# ---------- Nombres de estado propios en la OT (WO-02 CA5) ----------


@pytest.fixture
def custom_labels(tenant_a):
    tenant_a.org.work_order_status_labels = {"in_progress": "En faena", "planned": "Por hacer"}
    tenant_a.org.save()
    return tenant_a


def test_transition_buttons_filter_and_flash_use_the_custom_labels(client, custom_labels):
    tenant_a = custom_labels
    with tenant_context(tenant_a.org):
        wo = create_work_order(work_type="corrective", title="x", asset=tenant_a.asset, status="requested")
    login(client, tenant_a.admin)
    detail = client.get(reverse("work_order_detail", args=[wo.pk]))
    assert ("planned", "Por hacer") in detail.context["transitions"]
    choices = dict(client.get(reverse("work_order_list")).context["filter_form"].fields["status"].choices)
    assert choices["in_progress"] == "En faena" and choices["planned"] == "Por hacer"
    response = client.post(reverse("work_order_transition", args=[wo.pk]), {"to": "planned"}, follow=True)
    assert f"{wo.code}: Por hacer." in [str(m) for m in response.context["messages"]]


def test_invalid_transition_error_uses_the_custom_labels(custom_labels):
    tenant_a = custom_labels
    with tenant_context(tenant_a.org):
        wo = create_work_order(work_type="corrective", title="x", asset=tenant_a.asset)
        with pytest.raises(WorkOrderError) as error:
            transition(work_order=wo, to_status="closed", membership=tenant_a.admin)
    assert "Por hacer → Cerrada" in error.value.message


def test_report_problem_links_on_location_pages_and_asset_report(client, tenant_a):
    """Accesos a "Avisar problema" en ubicaciones y desde el aviso de un activo (REQ-01 CA1)."""
    with tenant_context(tenant_a.org):
        area = Location.objects.create(code="BOD", name="Bodega")
        Asset.objects.filter(pk=tenant_a.asset.pk).update(location=area)
    report_url = reverse("location_report")
    login(client, tenant_a.admin)
    assert f'href="{report_url}"' in client.get(reverse("location_list")).content.decode()
    edit_page = client.get(reverse("location_edit", args=[area.pk])).content.decode()
    assert f'href="{report_url}?location={area.pk}"' in edit_page

    login(client, tenant_a.tech)
    page = client.get(reverse("asset_report", args=[tenant_a.asset.pk])).content.decode()
    assert f'href="{report_url}?location={area.pk}"' in page
    assert "¿No es un equipo? Avisar problema en una ubicación" in page
