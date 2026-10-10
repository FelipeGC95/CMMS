"""KPI-02: los mismos roles que ven el tablero, con su alcance por sitio y según el plan."""

import datetime

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import MembershipLocation
from apps.assets.models import Asset
from apps.core.tenancy import tenant_context
from apps.organizations.models import Location, Plan
from apps.organizations.plans import BASE_MODULES
from apps.work.models import FailureCode, FailureEvent, WorkOrder
from apps.work.services import create_work_order
from tests.conftest import login


@pytest.mark.parametrize("role", ["admin", "tech", "viewer"])
def test_every_role_that_sees_the_dashboard_sees_the_report(client, tenant_a, role):
    login(client, getattr(tenant_a, role))
    assert client.get(reverse("kpi_report")).status_code == 200
    assert client.get(reverse("kpi_report_export_xlsx")).status_code == 200
    assert reverse("kpi_report") in client.get(reverse("menu")).content.decode()


def test_anonymous_is_sent_to_login(client):
    assert client.get(reverse("kpi_report")).status_code == 302
    assert client.get(reverse("kpi_report_export_xlsx")).status_code == 302


@pytest.mark.parametrize("role", ["admin", "tech", "viewer"])
def test_plan_without_kpis_module_forbids_report_and_export(client, tenant_a, role):
    features = [m for m in BASE_MODULES if m != "kpis"] + ["exports"]
    tenant_a.org.plan = Plan.objects.create(code="sin-kpi", name="Sin KPI", features=features)
    tenant_a.org.save(update_fields=["plan"])
    login(client, getattr(tenant_a, role))
    assert client.get(reverse("kpi_report")).status_code == 403
    assert client.get(reverse("kpi_report_export_xlsx")).status_code == 403
    assert reverse("kpi_report") not in client.get(reverse("menu")).content.decode()
    dashboard = client.get(reverse("dashboard"))
    assert dashboard.status_code == 200 and reverse("kpi_report") not in dashboard.content.decode()


def test_plan_without_exports_hides_the_excel(client, tenant_a):
    tenant_a.org.plan = Plan.objects.create(code="base", name="Base", features=list(BASE_MODULES))
    tenant_a.org.save(update_fields=["plan"])
    login(client, tenant_a.admin)
    page = client.get(reverse("kpi_report"))
    assert page.status_code == 200
    assert reverse("kpi_report_export_xlsx") not in page.content.decode()
    assert client.get(reverse("kpi_report_export_xlsx")).status_code == 403


@pytest.fixture
def two_sites(tenant_a):
    """Norte y Sur, cada uno con una OT pendiente, una cerrada con costo y una falla con código.
    El técnico queda limitado a Norte."""
    yesterday = timezone.now() - datetime.timedelta(days=1)
    with tenant_context(tenant_a.org):
        mode = FailureCode.objects.create(kind="mode", code="ZZ", name="Modo")
        for code, cost in (("N", 100), ("S", 7000)):
            site = Location.objects.create(code=code, name=f"Planta {code}")
            asset = Asset.objects.create(
                code=f"{code}-01",
                name=f"Equipo {code}",
                asset_class=tenant_a.asset.asset_class,
                location=site,
            )
            create_work_order(work_type="corrective", title="Pendiente", asset=asset, estimated_minutes=60)
            done = create_work_order(work_type="corrective", title="Cerrada", asset=asset)
            WorkOrder.objects.filter(pk=done.pk).update(
                status="closed", completed_at=yesterday, closed_at=yesterday, total_cost=cost
            )
            FailureEvent.objects.create(
                asset=asset,
                impact="total_stop",
                failure_mode=mode,
                started_at=yesterday,
                ended_at=timezone.now(),
            )
            if code == "N":
                tenant_a.tech.restricted_to_locations = True
                tenant_a.tech.save(update_fields=["restricted_to_locations"])
                MembershipLocation.objects.create(membership=tenant_a.tech, location=site)
    return tenant_a


def test_scoped_technician_only_measures_its_sites(client, two_sites):
    login(client, two_sites.tech)
    context = client.get(reverse("kpi_report")).context
    assert context["backlog"].current.count == 1
    assert [r["asset__code"] for r in context["cost_rows"]] == ["N-01"]
    assert context["costs"].totals["total"] == 100
    assert [r.count for r in context["pareto"].rows] == [1]


def test_unscoped_admin_measures_both_sites(client, two_sites):
    login(client, two_sites.admin)
    context = client.get(reverse("kpi_report")).context
    assert context["backlog"].current.count == 2
    assert [r["asset__code"] for r in context["cost_rows"]] == ["S-01", "N-01"]
    assert [r.count for r in context["pareto"].rows] == [2]


def test_scoped_technician_cannot_filter_a_site_outside_its_scope(client, two_sites):
    login(client, two_sites.tech)
    with tenant_context(two_sites.org):
        south = Location.objects.get(code="S")
    response = client.get(reverse("kpi_report"), {"location": south.pk})
    assert "location" in response.context["filter_form"].errors
    assert response.context["costs"].totals["total"] == 100
