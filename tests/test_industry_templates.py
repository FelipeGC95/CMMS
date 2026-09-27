"""Biblioteca de plantillas por rubro (ORG-03 CA3, PM-07): archivos válidos y copia idempotente."""

import pytest
from django.core.exceptions import ValidationError

from apps.assets.models import AssetClass, AssetClassAttribute
from apps.core.tenancy import tenant_context
from apps.maintenance.industry_templates import apply_industry_template, preview_industry_template
from apps.maintenance.models import MaintenancePlan, PlanVersion
from apps.maintenance.templates_library import (
    INDUSTRIES,
    available_templates,
    load_template,
    validate_template,
)
from apps.work.models import FailureCode


@pytest.mark.parametrize("industry", INDUSTRIES)
def test_template_file_is_valid(industry):
    data = load_template(industry)
    assert data["industry"] == industry
    assert validate_template(data) == []
    # Tamaño revisable: 5 a 8 clases, 1 a 3 planes por clase.
    assert 5 <= len(data["asset_classes"]) <= 8
    assert all(1 <= len(c["plans"]) <= 3 for c in data["asset_classes"])
    # Todo es borrador hasta que el dueño lo revise.
    assert data["review_status"] == "draft"
    assert data["review_note"] == "Borrador — revisar antes del piloto"


def test_class_and_plan_codes_are_unique_across_the_library():
    classes, plans = [], []
    for industry in INDUSTRIES:
        for cls in load_template(industry)["asset_classes"]:
            classes.append(cls["code"].lower())
            plans += [p["code"].lower() for p in cls["plans"]]
    assert len(classes) == len(set(classes))
    assert len(plans) == len(set(plans))


def test_validation_reports_bad_content():
    data = {
        "industry": "x", "name": "X", "description": "d", "version": 1, "review_status": "draft",
        "review_note": "n",
        "asset_classes": [
            {
                "code": "A", "name": "A", "iso_category": "nope", "level": "equipment",
                "attributes": [
                    {"label": "L", "type": "choice"},
                    {"label": "Peso", "type": "text", "unit": "kg"},
                ],
                "plans": [
                    {
                        "code": "P", "name": "P", "meter_kind": "other", "meter_interval": 0,
                        "estimated_minutes": 10,
                        "items": [
                            {"text": "M", "type": "measurement", "unit": "bar", "min": 9, "max": 1},
                            {"text": "C", "type": "check", "unit": "bar"},
                        ],
                    },
                    {
                        "code": "p", "name": "Q", "estimated_minutes": 5,
                        "items": [{"text": "X", "type": "check"}],
                    },
                ],
            }
        ],
        "failure_codes": [{"kind": "mode", "code": "TOO-LONG-CODE", "name": "x"}],
    }  # fmt: skip
    errors = "\n".join(validate_template(data))
    for fragment in (
        "iso_category", "choices: lista", "unit: solo en atributos numéricos", "meter_kind",
        "meter_interval", "mínimo mayor", "unidad y rango solo en mediciones", "repetido",
        "sin frecuencia", "más de 10",
    ):  # fmt: skip
        assert fragment in errors


def test_available_templates_lists_the_four_industries():
    rows = available_templates()
    assert [r["industry"] for r in rows] == list(INDUSTRIES)
    assert all(r["is_draft"] and r["classes"] and r["plans"] and r["failure_codes"] for r in rows)


def test_apply_copies_classes_attributes_failures_and_published_plans(tenant_a):
    data = load_template("fleet")
    summary = apply_industry_template(organization=tenant_a.org, industry="fleet", by=tenant_a.admin.user)
    plans_total = sum(len(c["plans"]) for c in data["asset_classes"])
    assert len(summary.classes) == len(data["asset_classes"]) and summary.applied
    assert len(summary.plans) == plans_total
    with tenant_context(tenant_a.org):
        forklift = AssetClass.objects.get(code="GRH-COMB")
        assert forklift.iso_category == "vehicle" and forklift.level_hint == "equipment"
        fuel = AssetClassAttribute.objects.get(asset_class=forklift, key="combustible")
        assert fuel.data_type == "choice" and "Diésel" in fuel.choices
        plan = MaintenancePlan.objects.get(code="GRH-C-250H")
        version = plan.current_version
        assert plan.asset_class == forklift
        assert version.status == PlanVersion.Status.PUBLISHED and version.number == 1
        assert (version.meter_kind, version.meter_interval, version.calendar_interval) == ("hours", 250, 3)
        assert version.published_by == tenant_a.admin.user
        wear = version.items.get(item_type="measurement")
        assert (wear.unit, wear.min_value, wear.max_value) == ("%", 0, 10)
        assert version.resources.count() == 3
        photo = MaintenancePlan.objects.get(code="GRH-C-1000H").current_version.items.get(item_type="photo")
        assert photo.required
        assert FailureCode.objects.filter(kind="mode", code="FTS").exists()
        assert (
            MaintenancePlan.objects.filter(code__in=[p["code"] for p in summary.plans]).count() == plans_total
        )


def test_apply_is_idempotent_and_never_overwrites(tenant_a):
    with tenant_context(tenant_a.org):
        mine = AssetClass.objects.create(code="CAMION", name="Mis camiones", iso_category="other")
    apply_industry_template(organization=tenant_a.org, industry="fleet")
    again = apply_industry_template(organization=tenant_a.org, industry="fleet")
    assert again.is_empty and again.plans_skipped and again.classes_skipped
    with tenant_context(tenant_a.org):
        mine.refresh_from_db()
        assert (mine.name, mine.iso_category) == ("Mis camiones", "other")
        assert not mine.attributes.exists()  # la clase propia no recibe atributos
        # El plan de camión igual se crea y queda ligado a la clase existente del mismo código.
        assert MaintenancePlan.objects.get(code="CAM-15000KM").asset_class == mine
        assert AssetClass.objects.filter(code="CAMION").count() == 1


def test_preview_does_not_write_and_matches_apply(tenant_a):
    preview = preview_industry_template(organization=tenant_a.org, industry="food")
    with tenant_context(tenant_a.org):
        assert not MaintenancePlan.objects.filter(code="ENV-500K").exists()
    assert not preview.applied
    applied = apply_industry_template(organization=tenant_a.org, industry="food")
    assert [p["code"] for p in preview.plans] == [p["code"] for p in applied.plans]
    assert preview.attributes == applied.attributes
    packer = next(p for p in preview.plans if p["code"] == "ENV-500K")
    assert packer["frequency_label"] == "500.000 ciclos o 3 meses"


def test_parts_and_plan_codes_limit_what_is_copied(tenant_a):
    summary = apply_industry_template(
        organization=tenant_a.org, industry="buildings", parts=("classes", "failure_codes")
    )
    assert summary.classes and not summary.plans
    with tenant_context(tenant_a.org):
        assert not MaintenancePlan.objects.filter(code="GEN-MENS").exists()
    summary = apply_industry_template(
        organization=tenant_a.org, industry="buildings", parts=("plans",), plan_codes={"GEN-MENS"}
    )
    assert [p["code"] for p in summary.plans] == ["GEN-MENS"]
    with tenant_context(tenant_a.org):
        assert MaintenancePlan.objects.get(code="GEN-MENS").asset_class.code == "GENERADOR"


def test_unknown_industry_or_part_is_rejected(tenant_a):
    with pytest.raises(ValidationError):
        apply_industry_template(organization=tenant_a.org, industry="mineria")
    with pytest.raises(ValidationError):
        apply_industry_template(organization=tenant_a.org, industry="fleet", parts=("assets",))


def test_apply_query_count_does_not_grow_with_the_template(tenant_a, django_assert_max_num_queries):
    # Rubro completo (7 clases, 13 planes, ~90 ítems): consultas por lotes.
    with django_assert_max_num_queries(30):
        apply_industry_template(organization=tenant_a.org, industry="industrial", by=tenant_a.admin.user)
