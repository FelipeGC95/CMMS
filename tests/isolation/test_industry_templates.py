"""Plantillas de rubro: aplicar una plantilla a A nunca toca ni mira los datos de B."""

from django.urls import reverse

from apps.assets.models import AssetClass
from apps.core.tenancy import tenant_context
from apps.maintenance.industry_templates import apply_industry_template, preview_industry_template
from apps.maintenance.models import MaintenancePlan
from apps.work.models import FailureCode
from tests.conftest import login


def _counts(tenant):
    with tenant_context(tenant.org):
        return (AssetClass.objects.count(), MaintenancePlan.objects.count(), FailureCode.objects.count())


def test_applying_to_a_never_touches_b(tenant_a, tenant_b):
    before = _counts(tenant_b)
    summary = apply_industry_template(organization=tenant_a.org, industry="industrial")
    assert summary.plans and _counts(tenant_b) == before
    with tenant_context(tenant_a.org):
        assert not AssetClass.objects.exclude(organization=tenant_a.org).exists()
        assert all(p.organization_id == tenant_a.org.id for p in MaintenancePlan.objects.all())


def test_b_existing_codes_do_not_hide_anything_from_a(tenant_a, tenant_b):
    """Lo que B ya tiene no cuenta como «ya existe» para A."""
    apply_industry_template(organization=tenant_b.org, industry="fleet")
    preview = preview_industry_template(organization=tenant_a.org, industry="fleet")
    assert not preview.classes_skipped and not preview.plans_skipped
    apply_industry_template(organization=tenant_a.org, industry="fleet")
    with tenant_context(tenant_a.org):
        plan = MaintenancePlan.objects.get(code="GRH-C-250H")
        assert plan.asset_class.organization_id == tenant_a.org.id


def test_web_action_applies_only_to_the_session_organization(client, tenant_a, tenant_b):
    before = _counts(tenant_b)
    login(client, tenant_a.admin)
    client.post(reverse("industry_template", args=["food"]))
    assert _counts(tenant_b) == before
    with tenant_context(tenant_a.org):
        assert MaintenancePlan.objects.filter(code="ENV-500K").exists()
