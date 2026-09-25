"""Alcance por sitio (ORG-06 CA2): una membresía limitada solo ve activos, OT e indicadores de sus sitios."""

from dataclasses import dataclass

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from apps.accounts.models import Membership, MembershipLocation
from apps.assets.models import Asset
from apps.core.tenancy import tenant_context
from apps.organizations.models import Location
from apps.work.models import WorkOrder
from apps.work.services import create_work_order, report_failure
from tests.conftest import login


@dataclass
class Scene:
    inside: Asset
    outside: Asset
    wo_inside: WorkOrder
    wo_outside: WorkOrder


@pytest.fixture
def scene(tenant_a):
    """Planta Norte (con su área) y Planta Sur; el técnico queda limitado a Planta Norte."""
    with tenant_context(tenant_a.org):
        north = Location.objects.create(code="N", name="Planta Norte")
        north_area = Location.objects.create(code="N-A", name="Envasado", kind="area", parent=north)
        south = Location.objects.create(code="S", name="Planta Sur")
        cls = tenant_a.asset.asset_class
        inside = Asset.objects.create(code="IN-01", name="Llenadora", asset_class=cls, location=north_area)
        outside = Asset.objects.create(code="OUT-01", name="Caldera", asset_class=cls, location=south)
        wo_inside = create_work_order(
            work_type="preventive", title="Dentro", asset=inside, checklist=tenant_a.plan.items.all()
        )
        wo_outside = create_work_order(
            work_type="preventive", title="Fuera", asset=outside, checklist=tenant_a.plan.items.all()
        )
        MembershipLocation.objects.create(membership=tenant_a.tech, location=north)
    tenant_a.tech.restricted_to_locations = True
    tenant_a.tech.save()
    return Scene(inside, outside, wo_inside, wo_outside)


def test_asset_list_only_shows_assets_in_scope(client, tenant_a, scene):
    login(client, tenant_a.tech)
    codes = {a.code for a in client.get(reverse("asset_list")).context["page"].object_list}
    assert codes == {"IN-01"}  # el activo sin ubicación de conftest tampoco se ve


def test_unrestricted_member_sees_everything(client, tenant_a, scene):
    login(client, tenant_a.admin)
    codes = {a.code for a in client.get(reverse("asset_list")).context["page"].object_list}
    assert {"IN-01", "OUT-01", tenant_a.asset.code} <= codes


@pytest.mark.parametrize("url_name", ["asset_detail", "asset_report"])
def test_asset_pages_out_of_scope_are_404(client, tenant_a, scene, url_name):
    login(client, tenant_a.tech)
    assert client.get(reverse(url_name, args=[scene.inside.pk])).status_code == 200
    assert client.get(reverse(url_name, args=[scene.outside.pk])).status_code == 404


def test_qr_of_asset_out_of_scope_is_404_without_redirect_loop(client, tenant_a, scene):
    login(client, tenant_a.tech)
    assert client.get(reverse("asset_qr", args=[scene.outside.qr_token])).status_code == 404
    response = client.get(reverse("asset_qr", args=[scene.inside.qr_token]))
    assert response.status_code == 302 and response["Location"] == reverse(
        "asset_report", args=[scene.inside.pk]
    )


def test_work_orders_out_of_scope_are_hidden(client, tenant_a, scene):
    login(client, tenant_a.tech)
    # scope=open: el técnico ve "Mis OT" por defecto (WO-05), no es lo que prueba este test.
    listed = {
        wo.pk for wo in client.get(reverse("work_order_list"), {"scope": "open"}).context["page"].object_list
    }
    assert scene.wo_inside.pk in listed and scene.wo_outside.pk not in listed
    assert client.get(reverse("work_order_detail", args=[scene.wo_outside.pk])).status_code == 404
    with tenant_context(tenant_a.org):
        item = scene.wo_outside.checklist.first()
    assert (
        client.post(
            reverse("work_order_transition", args=[scene.wo_outside.pk]), {"to": "scheduled"}
        ).status_code
        == 404
    )
    assert (
        client.post(
            reverse("checklist_answer", args=[scene.wo_outside.pk, item.pk]), {"result": "ok"}
        ).status_code
        == 404
    )
    assert client.post(reverse("work_order_complete", args=[scene.wo_outside.pk]), {}).status_code == 404


def test_scoped_technician_cannot_filter_by_a_site_outside_scope(client, tenant_a, scene):
    """El sitio "Planta Sur" es de la propia organización pero fuera del alcance del técnico (WO-05)."""
    login(client, tenant_a.tech)
    response = client.get(
        reverse("work_order_list"), {"scope": "open", "location": scene.outside.location_id}
    )
    assert response.status_code == 200
    assert scene.wo_outside.pk not in {wo.pk for wo in response.context["page"].object_list}
    # El filtro por responsable tampoco deja elegir a alguien fuera de su alcance más allá de sí mismo.
    response = client.get(
        reverse("work_order_list"), {"scope": "open", "assigned_to": tenant_a.admin.user_id}
    )
    assert response.status_code == 200


def test_scoped_member_cannot_create_a_work_order_on_an_asset_outside_scope(client, tenant_a, scene):
    """El activo fuera de alcance no aparece en el select y tampoco se puede forzar por POST (WO-01)."""
    tenant_a.tech.role = Membership.Role.SUPERVISOR  # necesita work_order.manage para llegar al alta
    tenant_a.tech.save()
    login(client, tenant_a.tech)

    page = client.get(reverse("work_order_create")).content.decode()
    assert str(scene.inside.pk) in page
    assert str(scene.outside.pk) not in page

    with tenant_context(tenant_a.org):
        before = WorkOrder.objects.count()
    response = client.post(
        reverse("work_order_create"),
        {"work_type": "corrective", "asset": str(scene.outside.pk), "title": "x", "priority": "3"},
    )
    assert response.status_code == 200
    assert "asset" in response.context["form"].errors
    with tenant_context(tenant_a.org):
        assert WorkOrder.objects.count() == before


def test_attachment_out_of_scope_is_404(client, tenant_a, scene):
    photo = SimpleUploadedFile("f.jpg", b"\xff\xd8\xff fake", content_type="image/jpeg")
    with tenant_context(tenant_a.org):
        request = report_failure(
            membership=tenant_a.admin,
            asset=scene.outside,
            description="Fuga",
            asset_stopped=False,
            photo=photo,
        )
        attachment = request.work_order.attachments.get()
    login(client, tenant_a.tech)
    assert client.get(reverse("attachment", args=[attachment.pk])).status_code == 404


def test_dashboard_counts_only_scope(client, tenant_a, scene):
    with tenant_context(tenant_a.org):
        Asset.objects.filter(pk=scene.outside.pk).update(status=Asset.Status.STOPPED)
    login(client, tenant_a.tech)
    context = client.get(reverse("dashboard")).context
    assert context["kpis"].open_work_orders == 1
    assert list(context["down_assets"]) == []
    client.logout()
    login(client, tenant_a.admin)
    assert client.get(reverse("dashboard")).context["kpis"].open_work_orders == 2


def test_external_without_locations_sees_nothing(client, tenant_a, scene):
    external = Membership.objects.create(
        user=tenant_a.viewer.user.__class__.objects.create_user("contratista@a.cl"),
        organization=tenant_a.org,
        role=Membership.Role.EXTERNAL,
    )
    login(client, external)
    assert list(client.get(reverse("asset_list")).context["page"].object_list) == []
    assert list(client.get(reverse("work_order_list")).context["page"].object_list) == []


def test_scoped_asset_list_queries_do_not_grow(client, tenant_a, scene):
    login(client, tenant_a.tech)

    def count():
        with CaptureQueriesContext(connection) as ctx:
            client.get(reverse("asset_list"))
        return len(ctx.captured_queries)

    few = count()
    with tenant_context(tenant_a.org):
        north = Location.objects.get(code="N")
        for i in range(10):
            Asset.objects.create(
                code=f"X-{i}", name="x", asset_class=tenant_a.asset.asset_class, location=north
            )
    assert count() == few
