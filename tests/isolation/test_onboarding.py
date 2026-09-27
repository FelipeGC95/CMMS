"""ORG-03: el avance del asistente y lo que muestra son de cada organización."""

from decimal import Decimal

from django.urls import reverse

from apps.assets.models import Asset, AssetClass, Meter
from apps.core.tenancy import tenant_context
from apps.onboarding import services
from apps.onboarding.models import OnboardingState
from apps.organizations.models import Location
from tests.conftest import login


def _b_far_along(tenant_b):
    with tenant_context(tenant_b.org):
        services.choose_industry(membership=tenant_b.admin, industry="fleet")
        services.create_locations(membership=tenant_b.admin, site_name="Sitio B", areas=["Area B"])
        forklift = AssetClass.objects.get(code="GRH-COMB")
        services.quick_add_assets(
            membership=tenant_b.admin,
            rows=[{"name": "Grúa B", "asset_class": forklift, "location": None, "meter_kind": "hours"}],
        )
        services.create_suggested_plans(membership=tenant_b.admin, plan_codes=["GRH-C-250H"])
        services.dismiss(membership=tenant_b.admin)


def test_state_of_b_never_affects_a(client, tenant_a, tenant_b):
    _b_far_along(tenant_b)
    with tenant_context(tenant_a.org):
        assert not OnboardingState.objects.exists()
        assert services.suggested_plans(membership=tenant_a.admin) == []
        assert not services.meters_for_readings().exists()
        # A elige el mismo rubro: copia sus propias clases, sin chocar con las de B.
        services.choose_industry(membership=tenant_a.admin, industry="fleet")
        state = OnboardingState.objects.get()
        assert state.organization_id == tenant_a.org.id and state.dismissed_at is None
    login(client, tenant_a.admin)
    page = client.get(reverse("onboarding_step", args=["locations"])).content.decode()
    assert "Sitio B" not in page
    page = client.get(reverse("onboarding_step", args=["assets"])).content.decode()
    assert "Grúa B" not in page
    # A ve su propio avance (paso 2): el aviso descartado por B no lo oculta.
    assert "(paso 2 de 6)" in client.get(reverse("dashboard")).content.decode()
    with tenant_context(tenant_b.org):
        assert OnboardingState.objects.get().dismissed_at is not None


def test_a_cannot_reference_b_rows(client, tenant_a, tenant_b):
    _b_far_along(tenant_b)
    with tenant_context(tenant_b.org):
        b_class = AssetClass.objects.get(code="GRH-COMB")
        b_area = Location.objects.get(name="Area B")
        b_meter = Meter.objects.get()
    login(client, tenant_a.admin)
    data = {
        "rows-TOTAL_FORMS": "1",
        "rows-INITIAL_FORMS": "0",
        "rows-MIN_NUM_FORMS": "0",
        "rows-MAX_NUM_FORMS": "20",
        "rows-0-name": "Intruso",
        "rows-0-asset_class": b_class.pk,
        "rows-0-location": b_area.pk,
    }
    response = client.post(reverse("onboarding_step", args=["assets"]), data)
    assert response.status_code == 200  # la clase y la ubicación de B no son opciones válidas
    with tenant_context(tenant_a.org):
        assert not Asset.objects.filter(name="Intruso").exists()
        # Un medidor de B en las lecturas de A se ignora.
        assert (
            services.record_initial_readings(membership=tenant_a.admin, values={b_meter.pk: Decimal(5)}) == 0
        )
    with tenant_context(tenant_b.org):
        assert Meter.objects.get().current_value is None
