"""Interfaz coherente con la solo lectura (ORG-13 CA5, ADR-033): una organización en solo lectura
no ve botones, formularios ni accesos de escritura; sí ve exportar, PDF, etiquetas y Facturación.
Una organización al día (o exenta) ve todo como siempre."""

import datetime

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.accounts.permissions import Perm
from apps.billing.access import WRITE_PERMS, can_act
from apps.core.tenancy import tenant_context
from apps.organizations.models import Organization, Plan
from apps.work.services import create_work_order, report_failure
from tests.api_utils import FIELD_API, field_client
from tests.billing_utils import make_billable
from tests.conftest import login

pytestmark = pytest.mark.django_db


def _use_pro(tenant):
    Organization.objects.filter(pk=tenant.org.pk).update(plan=Plan.objects.get(code="pro"))
    tenant.org.refresh_from_db()


def _lock(tenant):
    """Prueba vencida hace 30 días: gracia agotada → solo lectura."""
    make_billable(tenant.org)
    Organization.objects.filter(pk=tenant.org.pk).update(
        trial_ends_at=timezone.now() - datetime.timedelta(days=30), plan=Plan.objects.get(code="pro")
    )
    tenant.org.refresh_from_db()


@pytest.fixture(params=["read_only", "active"])
def org_state(request, tenant_a):
    if request.param == "read_only":
        _lock(tenant_a)
    else:
        _use_pro(tenant_a)
    with tenant_context(tenant_a.org):
        tenant_a.work_order = create_work_order(
            work_type="corrective", title="Fuga", asset=tenant_a.asset, assigned_to=tenant_a.admin.user
        )
        report_failure(membership=tenant_a.tech, description="Ruido", asset=tenant_a.asset)
    return request.param, tenant_a


def _pages(tenant):
    """(URL, enlaces de escritura que se ocultan en solo lectura, enlaces que siempre se ven)."""
    asset, wo, plan = tenant.asset, tenant.work_order, tenant.plan
    return [
        (
            reverse("asset_list"),
            [reverse("asset_create"), reverse("asset_import"), reverse("reading_import")],
            [reverse("asset_export_xlsx"), "labels=1", reverse("location_list")],
        ),
        (
            reverse("asset_detail", args=[asset.pk]),
            [
                reverse("asset_edit", args=[asset.pk]),
                reverse("asset_status", args=[asset.pk]),
                reverse("asset_report", args=[asset.pk]),
                reverse("asset_decommission", args=[asset.pk]),
                reverse("asset_attachment_add", args=[asset.pk]),
                reverse("asset_plan_assign", args=[asset.pk]),
            ],
            [reverse("asset_pdf", args=[asset.pk])],
        ),
        (
            reverse("work_order_list"),
            [reverse("work_order_create"), reverse("location_report")],
            [reverse("work_order_export_xlsx")],
        ),
        (
            reverse("work_order_detail", args=[wo.pk]),
            [
                reverse("work_order_transition", args=[wo.pk]),
                reverse("work_order_labor_add", args=[wo.pk]),
                reverse("work_order_part_add", args=[wo.pk]),
                reverse("work_order_assignees", args=[wo.pk]),
            ],
            [reverse("work_order_pdf", args=[wo.pk])],
        ),
        (reverse("request_list"), ["/accept/", "/reject/"], [reverse("request_list")]),
        (
            reverse("plan_list"),
            [reverse("plan_create"), reverse("industry_templates")],
            [reverse("plan_import"), reverse("plan_calendar")],
        ),
        (
            reverse("plan_detail", args=[plan.pk]),
            [reverse("plan_draft_start", args=[plan.pk]), reverse("plan_assign", args=[plan.pk])],
            [reverse("plan_list")],
        ),
        (
            reverse("inventory_home"),
            [reverse("inventory_part_create"), reverse("inventory_import")],
            [reverse("inventory_home")],
        ),
        (reverse("dashboard"), ["?report=1"], [reverse("work_order_list")]),
        (
            reverse("menu"),
            [reverse("asset_create"), reverse("location_report"), reverse("inventory_import")],
            [
                reverse("billing"),
                reverse("organization_settings"),
                reverse("request_list"),
                reverse("plan_list"),
                reverse("plan_import"),
                reverse("work_order_board"),
                reverse("location_list"),
                reverse("asset_class_list"),
                reverse("team"),
            ],
        ),
        (reverse("failure_code_list"), [reverse("failure_code_create")], []),
        (reverse("location_list"), ['id="new-title"'], []),
    ]


def test_write_actions_follow_the_billing_state(client, org_state):
    state, tenant = org_state
    login(client, tenant.admin)
    for url, writes, reads in _pages(tenant):
        response = client.get(url)
        assert response.status_code == 200, url
        html = response.content.decode()
        for write in writes:
            if state == "read_only":
                assert write not in html, (url, write)
            else:
                assert write in html, (url, write)
        for read in reads:
            assert read in html, (url, read)


def test_admin_keeps_the_pay_banner_and_billing(client, tenant_a):
    _lock(tenant_a)
    login(client, tenant_a.admin)
    html = client.get(reverse("asset_list")).content.decode()
    assert "solo lectura por falta de pago" in html and reverse("billing") in html
    assert client.get(reverse("billing")).status_code == 200


def test_technician_sees_no_actions_on_the_work_order(client, tenant_a):
    _lock(tenant_a)
    with tenant_context(tenant_a.org):
        wo = create_work_order(
            work_type="preventive",
            title="PM",
            asset=tenant_a.asset,
            assigned_to=tenant_a.tech.user,
            checklist=tenant_a.plan.items.all(),
        )
    login(client, tenant_a.tech)
    html = client.get(reverse("work_order_detail", args=[wo.pk])).content.decode()
    assert reverse("work_order_transition", args=[wo.pk]) not in html
    assert reverse("work_order_complete", args=[wo.pk]) not in html
    assert "checklist_answer" not in html and f"/work-orders/{wo.pk}/checklist/" not in html
    assert "Purgar" in html  # el checklist se sigue viendo


def test_can_act_only_hides_write_permissions(tenant_a):
    _lock(tenant_a)
    with tenant_context(tenant_a.org):
        for perm in Perm:
            expected = perm not in WRITE_PERMS
            assert can_act(tenant_a.admin, perm) is expected, perm
    # Consulta y Facturación siguen vigentes.
    assert Perm.ORGANIZATION_MANAGE not in WRITE_PERMS and Perm.INVENTORY_VIEW not in WRITE_PERMS


def test_exempt_organization_keeps_every_permission(tenant_a):
    Organization.objects.filter(pk=tenant_a.org.pk).update(
        trial_ends_at=timezone.now() - datetime.timedelta(days=90)
    )
    tenant_a.org.refresh_from_db()
    with tenant_context(tenant_a.org):
        assert all(can_act(tenant_a.admin, perm) for perm in Perm)


def test_field_bootstrap_reports_read_only(tenant_a):
    assert field_client(tenant_a.tech).get(f"{FIELD_API}/bootstrap/").json()["read_only"] is False
    _lock(tenant_a)
    assert field_client(tenant_a.tech).get(f"{FIELD_API}/bootstrap/").json()["read_only"] is True
