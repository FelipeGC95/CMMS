"""ORG-03: asistente de puesta en marcha (pasos, saltar y retomar, idempotencia, flujo completo)."""

import datetime
from decimal import Decimal

import pytest
from django.core.cache import cache
from django.urls import reverse

from apps.assets.models import Asset, AssetClass, Meter, MeterReading
from apps.core.tenancy import tenant_context
from apps.maintenance.models import MaintenancePlan, PlanAssignment
from apps.onboarding import services
from apps.onboarding.models import OnboardingState, Step
from apps.onboarding.selectors import onboarding_for_dashboard
from apps.organizations.models import Location, Organization
from apps.work.models import FailureCode, WorkOrder
from tests.conftest import login
from tests.test_mfa import enable_totp
from tests.test_signup import confirm_url_from_mail, form_data

FLEET_PLANS = {"GRH-C-250H", "GRH-C-1000H"}


def _fleet_with_forklifts(membership, n=2):
    services.choose_industry(membership=membership, industry="fleet")
    forklift = AssetClass.objects.get(code="GRH-COMB")
    rows = [
        {"name": f"Grúa {i}", "asset_class": forklift, "location": None, "meter_kind": "hours"}
        for i in range(1, n + 1)
    ]
    services.quick_add_assets(membership=membership, rows=rows)
    return forklift


def test_industry_copies_classes_and_failure_codes_but_no_plans(in_a):
    admin = in_a.admin
    summary = services.choose_industry(membership=admin, industry="fleet")
    classes, codes = AssetClass.objects.count(), FailureCode.objects.count()
    assert summary.classes and summary.failure_codes and not summary.plans
    assert AssetClass.objects.filter(code="GRH-COMB").exists()
    assert not MaintenancePlan.objects.filter(code__in=FLEET_PLANS).exists()
    state = OnboardingState.objects.get()
    assert state.industry == "fleet" and state.completed_steps == ["industry"]
    assert state.current_step == Step.LOCATIONS
    # Repetir no duplica nada.
    services.choose_industry(membership=admin, industry="fleet")
    assert (AssetClass.objects.count(), FailureCode.objects.count()) == (classes, codes)
    assert OnboardingState.objects.get().completed_steps == ["industry"]


def test_locations_get_codes_and_repeat_does_not_duplicate(in_a):
    created = services.create_locations(
        membership=in_a.admin, site_name="Planta Sur", areas=["Producción", "Bodega"]
    )
    site = created[0]
    assert site.kind == Location.Kind.SITE and site.code == "PLANTA-SUR"
    assert {loc.code for loc in created[1:]} == {"PLANTA-SUR-PRODUCCION", "PLANTA-SUR-BODEGA"}
    assert all(loc.parent_id == site.pk for loc in created[1:])
    services.create_locations(membership=in_a.admin, site_name="planta sur", areas=["Bodega", "Taller"])
    assert Location.objects.count() == 4
    assert OnboardingState.objects.get().current_step == Step.ASSETS


def test_quick_add_generates_codes_and_meters_and_skips_repeats(in_a):
    forklift = _fleet_with_forklifts(in_a.admin)
    assets = Asset.objects.filter(asset_class=forklift).order_by("code")
    assert [a.code for a in assets] == ["GRH-COMB-01", "GRH-COMB-02"]
    assert Meter.objects.filter(asset__in=assets, kind="hours").count() == 2
    created, skipped = services.quick_add_assets(
        membership=in_a.admin,
        rows=[{"name": "Grúa 1", "asset_class": forklift, "location": None, "meter_kind": "hours"}],
    )
    assert created == [] and skipped == ["Grúa 1"]
    assert Asset.objects.filter(asset_class=forklift).count() == 2


def test_suggested_plans_only_for_present_classes_and_assigned_automatically(in_a):
    forklift = _fleet_with_forklifts(in_a.admin)
    suggested = services.suggested_plans(membership=in_a.admin)
    assert {p["code"] for p in suggested} == FLEET_PLANS  # solo la clase que tiene activos
    assert all(p["asset_count"] == 2 for p in suggested)

    created, assigned, warnings = services.create_suggested_plans(
        membership=in_a.admin, plan_codes=["GRH-C-250H"]
    )
    assert (created, assigned, warnings) == (1, 2, [])
    assert set(MaintenancePlan.objects.filter(code__in=FLEET_PLANS).values_list("code", flat=True)) == {
        "GRH-C-250H"
    }
    assert PlanAssignment.objects.filter(plan__code="GRH-C-250H", asset__asset_class=forklift).count() == 2
    # Repetir: no duplica planes ni reinicia asignaciones; un plan ya creado sigue sugerido.
    again = services.create_suggested_plans(membership=in_a.admin, plan_codes=["GRH-C-250H", "NOPE"])
    assert again == (0, 0, [])
    assert {p["code"]: p["exists"] for p in services.suggested_plans(membership=in_a.admin)} == {
        "GRH-C-250H": True,
        "GRH-C-1000H": False,
    }


def test_meter_plans_create_missing_meters(in_a):
    services.choose_industry(membership=in_a.admin, industry="fleet")
    forklift = AssetClass.objects.get(code="GRH-COMB")
    services.quick_add_assets(
        membership=in_a.admin, rows=[{"name": "Sin horómetro", "asset_class": forklift, "location": None}]
    )
    _created, assigned, warnings = services.create_suggested_plans(
        membership=in_a.admin, plan_codes=["GRH-C-250H"]
    )
    assert assigned == 1 and warnings == []
    assert Meter.objects.filter(asset__name="Sin horómetro", kind="hours").exists()


def test_initial_readings_rebase_meter_plans_without_duplicates(in_a):
    _fleet_with_forklifts(in_a.admin, n=1)
    services.create_suggested_plans(membership=in_a.admin, plan_codes=["GRH-C-250H"])
    meter = services.meters_for_readings().get()
    assert PlanAssignment.objects.get(meter=meter).next_due_meter_value == Decimal(250)

    assert services.record_initial_readings(membership=in_a.admin, values={meter.pk: Decimal(1200)}) == 1
    assignment = PlanAssignment.objects.get(meter=meter)
    assert assignment.next_due_meter_value == Decimal(1450)  # desde la lectura, no vencido contra 0
    assert not WorkOrder.objects.filter(plan_assignment=assignment).exists()
    # El mismo valor otra vez no crea otra lectura; en blanco, no hace nada.
    assert services.record_initial_readings(membership=in_a.admin, values={meter.pk: Decimal(1200)}) == 0
    assert services.record_initial_readings(membership=in_a.admin, values={meter.pk: None}) == 0
    assert MeterReading.objects.filter(meter=meter).count() == 1
    assert OnboardingState.objects.get().current_step == Step.CALENDAR


def test_skip_and_resume(in_a):
    services.skip_step(membership=in_a.admin, step=Step.INDUSTRY)
    state = services.skip_step(membership=in_a.admin, step=Step.LOCATIONS)
    assert state.skipped_steps == ["industry", "locations"] and state.current_step == Step.ASSETS
    assert state.finished_at is None
    # Se retoma un paso saltado: pasa a completado.
    services.create_locations(membership=in_a.admin, site_name="Faena Norte")
    state = OnboardingState.objects.get()
    assert state.skipped_steps == ["industry"] and state.completed_steps == ["locations"]
    services.skip_step(membership=in_a.admin, step=Step.CALENDAR)
    assert OnboardingState.objects.get().finished_at is not None


def test_calendar_summary_and_finish(in_a):
    _fleet_with_forklifts(in_a.admin)
    services.create_suggested_plans(membership=in_a.admin, plan_codes=["GRH-C-250H"])
    today = datetime.date.today()
    # Uno vence en 20 días; el otro queda a 3 meses (fuera de los 90 días o justo en el borde).
    first = PlanAssignment.objects.order_by("asset__code").first()
    PlanAssignment.objects.filter(pk=first.pk).update(next_due_on=today + datetime.timedelta(days=20))
    summary = services.calendar_summary(membership=in_a.admin, today=today)
    assert summary["count_30"] == 1 and summary["count_90"] >= 1 and summary["assignments"] == 2
    assert summary["upcoming"][0].asset.pk == first.asset_id
    # La lista de próximos mira 12 meses: el trimestral del otro activo también aparece.
    assert summary["count_365"] >= 2 and len(summary["upcoming"]) >= 2
    assert onboarding_for_dashboard(membership=in_a.admin) is not None
    services.finish(membership=in_a.admin)
    assert onboarding_for_dashboard(membership=in_a.admin) is None


def test_existing_organization_is_not_forced_into_the_wizard(client, tenant_a):
    login(client, tenant_a.admin)
    response = client.get(reverse("dashboard"))
    assert response.status_code == 200 and response.context["onboarding"] is None
    assert b"Abrir el asistente" in client.get(reverse("organization_settings")).content


def test_banner_until_dismissed(client, tenant_a):
    login(client, tenant_a.admin)
    client.get(reverse("onboarding"))  # abrirlo desde Configuración
    page = client.get(reverse("dashboard")).content.decode()
    assert "Continúa la puesta en marcha (paso 1 de 6)" in page
    client.post(reverse("onboarding_dismiss"))
    assert "Continúa la puesta en marcha" not in client.get(reverse("dashboard")).content.decode()


def test_excel_import_returns_to_the_wizard(client, tenant_a):
    login(client, tenant_a.admin)
    client.get(reverse("asset_import") + "?from=onboarding")
    assert client.session["asset_import_return"] == reverse("onboarding_step", args=["assets"])
    client.get(reverse("asset_import"))  # abierta desde Activos: vuelve a la lista
    assert "asset_import_return" not in client.session


@pytest.mark.django_db
def test_happy_path_from_signup_to_calendar(client, settings):
    """Registro → verificación → segundo factor (política por defecto) → asistente → calendario."""
    settings.SIGNUP_MIN_SECONDS = 0
    settings.MFA_ENFORCE = True
    cache.clear()
    client.post(reverse("signup"), form_data(client))
    response = client.post(confirm_url_from_mail())
    assert response["Location"] == reverse("onboarding")
    # Administrador sin segundo factor: primero lo configura.
    assert client.get(reverse("onboarding"))["Location"] == reverse("mfa_activate_totp")
    org = Organization.objects.get()
    enable_totp(org.memberships.get().user)
    # Tras configurarlo, «Ir al inicio» lleva al asistente (aún no abierto).
    assert client.get(reverse("dashboard"))["Location"] == reverse("onboarding")
    assert client.get(reverse("onboarding"))["Location"] == reverse("onboarding_step", args=["industry"])
    step = lambda name: reverse("onboarding_step", args=[name])  # noqa: E731

    assert client.get(step("industry")).status_code == 200
    assert client.post(step("industry"), {"industry": "fleet"})["Location"] == step("locations")
    assert client.get(step("locations")).status_code == 200
    response = client.post(
        step("locations"), {"site_name": "Centro de distribución", "areas": "Patio\nTaller"}
    )
    assert response["Location"] == step("assets")
    with tenant_context(org):
        forklift = AssetClass.objects.get(code="GRH-COMB")
        patio = Location.objects.get(name="Patio")
    assert client.get(step("assets")).status_code == 200
    rows = {
        "rows-TOTAL_FORMS": "5",
        "rows-INITIAL_FORMS": "0",
        "rows-MIN_NUM_FORMS": "0",
        "rows-MAX_NUM_FORMS": "20",
    }
    rows.update(
        {
            "rows-0-name": "Grúa 1",
            "rows-0-asset_class": forklift.pk,
            "rows-0-location": patio.pk,
            "rows-0-meter_kind": "hours",
        }
    )
    assert client.post(step("assets"), {**rows, "action": "continue"})["Location"] == step("plans")
    page = client.get(step("plans"))
    assert {p["code"] for p in page.context["plans"]} == FLEET_PLANS
    assert client.post(step("plans"), {"plan_codes": ["GRH-C-250H"]})["Location"] == step("readings")
    page = client.get(step("readings"))
    meter = page.context["form"].meters[0]
    assert client.post(step("readings"), {f"meter_{meter.pk}": "830"})["Location"] == step("calendar")
    page = client.get(step("calendar"))
    assert page.status_code == 200 and page.context["summary"]["assignments"] == 1
    assert client.post(step("calendar"))["Location"] == reverse("dashboard")

    with tenant_context(org):
        state = OnboardingState.objects.get()
        assert state.completed_steps == [s.value for s in Step] and state.finished_at is not None
        assert PlanAssignment.objects.get().next_due_meter_value == Decimal(1080)
    home = client.get(reverse("dashboard"))
    assert home.status_code == 200 and home.context["onboarding"] is None
