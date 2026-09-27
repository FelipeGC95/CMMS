"""Copia de una plantilla de rubro a una organización (ORG-03 CA3, PM-07, ADR-021).

`preview_industry_template` dice qué se crearía sin escribir nada;
`apply_industry_template` lo crea. Ambas son idempotentes y nunca sobrescriben
datos de la organización: una clase, un código de falla o un plan cuyo código
ya existe se omite. Lo copiado es de la organización (copias editables, no
referencias a la plantilla). Los planes quedan con su versión 1 publicada.

El asistente de puesta en marcha (ORG-03) usa estas mismas funciones:
`parts` elige qué copiar (el paso «rubro» copia clases y catálogo de fallas;
el paso «plantillas sugeridas» copia planes) y `plan_codes` limita los planes
a los que la persona eligió.

La escritura va por lotes (bulk_create): el número de consultas no crece con
el tamaño de la plantilla.
"""

from dataclasses import dataclass, field
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils.text import slugify
from django.utils.translation import gettext as _

from apps.assets.models import AssetClass, AssetClassAttribute
from apps.audit import services as audit
from apps.audit.models import AuditEvent
from apps.core.tenancy import tenant_context
from apps.work.models import FailureCode

from .models import ItemType, MaintenancePlan, PlanVersion
from .services import bulk_create_published_plans
from .templates_library import TemplateNotFound, load_template

PARTS = ("classes", "failure_codes", "plans")
CHANGE_NOTE = "Copiada de la plantilla de rubro «%(name)s» v%(version)s"


@dataclass
class TemplateSummary:
    """Qué crea (o creó) la plantilla en la organización y qué omite por existir ya."""

    industry: str
    name: str
    version: int
    is_draft: bool
    review_note: str
    classes: list[dict] = field(default_factory=list)
    classes_skipped: list[str] = field(default_factory=list)
    attributes: int = 0
    failure_codes: list[dict] = field(default_factory=list)
    failure_codes_skipped: list[str] = field(default_factory=list)
    plans: list[dict] = field(default_factory=list)
    plans_skipped: list[str] = field(default_factory=list)
    applied: bool = False

    @property
    def is_empty(self):
        return not (self.classes or self.failure_codes or self.plans)


def _version_fields(plan: dict) -> dict:
    fields = {
        "estimated_minutes": plan["estimated_minutes"],
        "instructions": plan.get("instructions", ""),
        "schedule_mode": plan.get("schedule_mode", PlanVersion.ScheduleMode.FLOATING),
        "tolerance_pct": plan.get("tolerance_pct", 10),
    }
    if plan.get("calendar_interval"):
        fields["calendar_interval"] = plan["calendar_interval"]
        fields["calendar_unit"] = plan["calendar_unit"]
    if plan.get("meter_kind"):
        fields["meter_kind"] = plan["meter_kind"]
        fields["meter_interval"] = Decimal(str(plan["meter_interval"]))
    return fields


def _item_fields(item: dict) -> dict:
    measurement = item["type"] == ItemType.MEASUREMENT
    return {
        "text": item["text"],
        "item_type": item["type"],
        "unit": item.get("unit", "") if measurement else "",
        "min_value": Decimal(str(item["min"])) if measurement and "min" in item else None,
        "max_value": Decimal(str(item["max"])) if measurement and "max" in item else None,
        # Una foto obligatoria siempre es obligatoria (igual que al crear planes a mano).
        "required": True if item["type"] == ItemType.PHOTO else item.get("required", True),
    }


def _plan_rows(data, plan_codes):
    for cls in data["asset_classes"]:
        for plan in cls.get("plans", ()):
            if plan_codes is None or plan["code"] in plan_codes:
                yield cls, plan


def _summarize(data, *, industry, parts, plan_codes) -> TemplateSummary:
    """Compara la plantilla con la organización del contexto (3 consultas)."""
    summary = TemplateSummary(
        industry=industry,
        name=data["name"],
        version=data["version"],
        is_draft=data.get("review_status") == "draft",
        review_note=data.get("review_note", ""),
    )
    if "classes" in parts:
        existing = {c.lower() for c in AssetClass.objects.values_list("code", flat=True)}
        for cls in data["asset_classes"]:
            if cls["code"].lower() in existing:
                summary.classes_skipped.append(cls["code"])
                continue
            summary.classes.append(cls)
            summary.attributes += len(cls.get("attributes", ()))
    if "failure_codes" in parts:
        existing = {(k, c.upper()) for k, c in FailureCode.objects.values_list("kind", "code")}
        for failure in data.get("failure_codes", ()):
            if (failure["kind"], failure["code"].upper()) in existing:
                summary.failure_codes_skipped.append(failure["code"])
            else:
                summary.failure_codes.append(failure)
    if "plans" in parts:
        existing = {c.lower() for c in MaintenancePlan.objects.values_list("code", flat=True)}
        for cls, plan in _plan_rows(data, plan_codes):
            if plan["code"].lower() in existing:
                summary.plans_skipped.append(plan["code"])
                continue
            version = PlanVersion(**_version_fields(plan))
            summary.plans.append(
                {
                    **plan,
                    "class_code": cls["code"],
                    "class_name": cls["name"],
                    "frequency_label": str(version.frequency_label),
                }
            )
    return summary


def _load(industry, parts):
    try:
        data = load_template(industry)
    except TemplateNotFound as exc:
        raise ValidationError(_("Rubro desconocido: %(industry)s.") % {"industry": industry}) from exc
    unknown = set(parts) - set(PARTS)
    if unknown:
        raise ValidationError(_("Partes desconocidas: %(parts)s.") % {"parts": ", ".join(sorted(unknown))})
    return data


def preview_industry_template(
    *, organization, industry: str, parts=PARTS, plan_codes=None
) -> TemplateSummary:
    """Lo que `apply_industry_template` crearía hoy en la organización, sin escribir nada."""
    data = _load(industry, parts)
    with tenant_context(organization):
        return _summarize(data, industry=industry, parts=parts, plan_codes=plan_codes)


def _attribute_rows(organization, asset_class, attributes):
    rows, keys = [], set()
    for order, attribute in enumerate(attributes, start=1):
        base = slugify(attribute["label"]).replace("-", "_")[:40] or "atributo"
        key, n = base, 2
        while key in keys:
            key, n = f"{base}_{n}", n + 1
        keys.add(key)
        rows.append(
            AssetClassAttribute(
                organization=organization,
                asset_class=asset_class,
                key=key,
                label=attribute["label"],
                data_type=attribute["type"],
                unit=attribute.get("unit", ""),
                choices=list(attribute.get("choices", ())),
                required=attribute.get("required", False),
                order=order,
            )
        )
    return rows


@transaction.atomic
def apply_industry_template(
    *, organization, industry: str, by=None, parts=PARTS, plan_codes=None
) -> TemplateSummary:
    """Copia a la organización lo que aún no tiene de la plantilla del rubro.

    `by`: usuario que la aplica (auditoría y «publicada por»); None en comandos
    de plataforma. `parts`: subconjunto de PARTS. `plan_codes`: si se indica,
    solo esos planes. Devuelve el resumen con lo creado y lo omitido.
    """
    data = _load(industry, parts)
    with tenant_context(organization):
        summary = _summarize(data, industry=industry, parts=parts, plan_codes=plan_codes)
        summary.applied = True
        if summary.is_empty:
            return summary

        new_classes = AssetClass.objects.bulk_create(
            [
                AssetClass(
                    organization=organization,
                    code=cls["code"],
                    name=cls["name"],
                    iso_category=cls["iso_category"],
                    level_hint=cls["level"],
                )
                for cls in summary.classes
            ]
        )
        AssetClassAttribute.objects.bulk_create(
            [
                row
                for cls, asset_class in zip(summary.classes, new_classes, strict=True)
                for row in _attribute_rows(organization, asset_class, cls.get("attributes", ()))
            ]
        )
        FailureCode.objects.bulk_create(
            [
                FailureCode(organization=organization, kind=f["kind"], code=f["code"], name=f["name"])
                for f in summary.failure_codes
            ]
        )
        if summary.plans:
            _create_plans(organization, data, summary.plans, by=by)

        audit.record(
            action=AuditEvent.Action.CREATE,
            target=organization,
            actor=by,
            changes={
                "industry_template": [None, f"{industry} v{data['version']}"],
                "asset_classes": [None, [c["code"] for c in summary.classes]],
                "failure_codes": [None, [f"{f['kind']}:{f['code']}" for f in summary.failure_codes]],
                "plans": [None, [p["code"] for p in summary.plans]],
            },
        )
    return summary


def _create_plans(organization, data, plans, *, by):
    """Planes con su versión 1 ya publicada, por lotes (`services.bulk_create_published_plans`)."""
    classes = {c.code.lower(): c for c in AssetClass.objects.all()}
    bulk_create_published_plans(
        organization=organization,
        by=by,
        change_note=CHANGE_NOTE % {"name": data["name"], "version": data["version"]},
        plans=[
            {
                "code": plan["code"],
                "name": plan["name"],
                "asset_class": classes.get(plan["class_code"].lower()),
                "fields": _version_fields(plan),
                "items": [_item_fields(item) for item in plan["items"]],
                "resources": [
                    {
                        "kind": res["kind"],
                        "description": res["description"],
                        "quantity": Decimal(str(res.get("quantity", 1))),
                        "unit": res.get("unit", ""),
                    }
                    for res in plan.get("resources", ())
                ],
            }
            for plan in plans
        ],
    )
