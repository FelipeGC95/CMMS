"""Biblioteca de plantillas por rubro (ORG-03 CA3, PM-07, ADR-021).

Cada rubro es un archivo JSON versionado en el repositorio con clases de
activo (y sus atributos), planes preventivos por clase y códigos de falla
(ISO 14224). El contenido es un dato en español, no una cadena de interfaz:
no pasa por gettext. Al aplicarlo se copia a la organización
(`industry_templates.apply_industry_template`); la organización edita sus
copias sin afectar la plantilla.

`validate_template` revisa la estructura (tipos, unidades, medidores,
rangos y códigos únicos) y lo usa el test de la biblioteca.
"""

import json
from decimal import Decimal, InvalidOperation
from functools import cache
from pathlib import Path

from apps.assets.models import AssetClass, AttributeType, MeterKind

from ..models import CalendarUnit, ItemType, PlanResource, PlanVersion

LIBRARY_DIR = Path(__file__).resolve().parent
# Orden en que se ofrecen los rubros.
INDUSTRIES = ("fleet", "industrial", "food", "buildings")
MAX_ITEMS_PER_PLAN = 15


class TemplateNotFound(KeyError):
    pass


def template_path(industry: str) -> Path:
    return LIBRARY_DIR / f"{industry}.json"


@cache
def load_template(industry: str) -> dict:
    """Contenido del rubro tal como está en el archivo (sin validar: eso lo hace el test)."""
    if industry not in INDUSTRIES:
        raise TemplateNotFound(industry)
    with template_path(industry).open(encoding="utf-8") as handle:
        return json.load(handle)


def available_templates() -> list[dict]:
    """Rubros disponibles con sus totales, para elegir uno (web y asistente ORG-03)."""
    rows = []
    for industry in INDUSTRIES:
        data = load_template(industry)
        classes = data["asset_classes"]
        rows.append(
            {
                "industry": industry,
                "name": data["name"],
                "description": data["description"],
                "version": data["version"],
                "is_draft": data.get("review_status") == "draft",
                "review_note": data.get("review_note", ""),
                "classes": len(classes),
                "plans": sum(len(c.get("plans", ())) for c in classes),
                "failure_codes": len(data.get("failure_codes", ())),
            }
        )
    return rows


# --- Validación ---------------------------------------------------------------

LIMITS = {
    "class_code": 30,
    "class_name": 120,
    "attribute_label": 80,
    "attribute_unit": 20,
    "plan_code": 30,
    "plan_name": 150,
    "item_text": 250,
    "item_unit": 20,
    "resource_description": 200,
    "resource_unit": 20,
    "failure_code": 10,
    "failure_name": 120,
}
FAILURE_KINDS = ("mode", "cause", "mechanism", "detection_method")


def _number(value):
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError
    try:
        return Decimal(str(value))
    except InvalidOperation as exc:  # pragma: no cover - int/float siempre convierten
        raise ValueError from exc


def _text(errors, where, value, limit, *, required=True):
    if not isinstance(value, str) or (required and not value.strip()):
        errors.append(f"{where}: texto obligatorio")
    elif len(value) > LIMITS[limit]:
        errors.append(f"{where}: más de {LIMITS[limit]} caracteres")


def _validate_plan(errors, where, plan):
    _text(errors, f"{where}.code", plan.get("code"), "plan_code")
    _text(errors, f"{where}.name", plan.get("name"), "plan_name")
    has_calendar = plan.get("calendar_interval") is not None
    has_meter = plan.get("meter_kind") is not None or plan.get("meter_interval") is not None
    if not has_calendar and not has_meter:
        errors.append(f"{where}: sin frecuencia (calendario, medidor o ambas)")
    if has_calendar:
        interval = plan.get("calendar_interval")
        if not isinstance(interval, int) or isinstance(interval, bool) or interval < 1:
            errors.append(f"{where}.calendar_interval: entero positivo")
        if plan.get("calendar_unit") not in CalendarUnit.values:
            errors.append(f"{where}.calendar_unit: debe ser uno de {CalendarUnit.values}")
    if has_meter:
        if plan.get("meter_kind") not in MeterKind.values or plan.get("meter_kind") == MeterKind.OTHER:
            errors.append(f"{where}.meter_kind: debe ser hours, km o cycles")
        try:
            if _number(plan.get("meter_interval")) <= 0:
                raise ValueError
        except ValueError:
            errors.append(f"{where}.meter_interval: número positivo")
    if plan.get("schedule_mode", "floating") not in PlanVersion.ScheduleMode.values:
        errors.append(f"{where}.schedule_mode: floating o fixed")
    tolerance = plan.get("tolerance_pct", 10)
    if not isinstance(tolerance, int) or not 0 <= tolerance <= 100:
        errors.append(f"{where}.tolerance_pct: entero entre 0 y 100")
    minutes = plan.get("estimated_minutes")
    if not isinstance(minutes, int) or isinstance(minutes, bool) or minutes < 1:
        errors.append(f"{where}.estimated_minutes: entero positivo")
    if not isinstance(plan.get("instructions", ""), str):
        errors.append(f"{where}.instructions: texto")
    items = plan.get("items") or []
    if not 1 <= len(items) <= MAX_ITEMS_PER_PLAN:
        errors.append(f"{where}.items: entre 1 y {MAX_ITEMS_PER_PLAN} ítems")
    texts = set()
    for i, item in enumerate(items):
        at = f"{where}.items[{i}]"
        _text(errors, f"{at}.text", item.get("text"), "item_text")
        if item.get("text") in texts:
            errors.append(f"{at}.text: repetido en el plan")
        texts.add(item.get("text"))
        kind = item.get("type")
        if kind not in ItemType.values:
            errors.append(f"{at}.type: debe ser uno de {ItemType.values}")
        if not isinstance(item.get("required", True), bool):
            errors.append(f"{at}.required: true o false")
        has_range = any(k in item for k in ("unit", "min", "max"))
        if kind != ItemType.MEASUREMENT:
            if has_range:
                errors.append(f"{at}: unidad y rango solo en mediciones")
            continue
        _text(errors, f"{at}.unit", item.get("unit", ""), "item_unit", required=False)
        bounds = {}
        for key in ("min", "max"):
            if key in item:
                try:
                    bounds[key] = _number(item[key])
                except ValueError:
                    errors.append(f"{at}.{key}: número")
        if len(bounds) == 2 and bounds["min"] > bounds["max"]:
            errors.append(f"{at}: mínimo mayor que el máximo")
    for i, resource in enumerate(plan.get("resources") or []):
        at = f"{where}.resources[{i}]"
        if resource.get("kind") not in PlanResource.Kind.values:
            errors.append(f"{at}.kind: part o tool")
        _text(errors, f"{at}.description", resource.get("description"), "resource_description")
        _text(errors, f"{at}.unit", resource.get("unit", ""), "resource_unit", required=False)
        try:
            if _number(resource.get("quantity", 1)) <= 0:
                raise ValueError
        except ValueError:
            errors.append(f"{at}.quantity: número positivo")


def validate_template(data: dict) -> list[str]:
    """Errores de estructura de un rubro (lista vacía = válido)."""
    errors = []
    for key in ("industry", "name", "description", "review_note"):
        if not isinstance(data.get(key), str) or not data[key].strip():
            errors.append(f"{key}: texto obligatorio")
    if not isinstance(data.get("version"), int) or data["version"] < 1:
        errors.append("version: entero positivo")
    if data.get("review_status") not in ("draft", "reviewed"):
        errors.append("review_status: draft o reviewed")
    class_codes, plan_codes = set(), set()
    for c, cls in enumerate(data.get("asset_classes") or []):
        where = f"asset_classes[{c}]"
        _text(errors, f"{where}.code", cls.get("code"), "class_code")
        _text(errors, f"{where}.name", cls.get("name"), "class_name")
        code = str(cls.get("code", "")).lower()
        if code in class_codes:
            errors.append(f"{where}.code: {cls.get('code')} repetido")
        class_codes.add(code)
        if cls.get("iso_category") not in AssetClass.IsoCategory.values:
            errors.append(f"{where}.iso_category: debe ser uno de {AssetClass.IsoCategory.values}")
        if cls.get("level") not in AssetClass.Level.values:
            errors.append(f"{where}.level: debe ser uno de {AssetClass.Level.values}")
        labels = set()
        for a, attribute in enumerate(cls.get("attributes") or []):
            at = f"{where}.attributes[{a}]"
            _text(errors, f"{at}.label", attribute.get("label"), "attribute_label")
            if str(attribute.get("label")).lower() in labels:
                errors.append(f"{at}.label: repetido en la clase")
            labels.add(str(attribute.get("label")).lower())
            kind = attribute.get("type")
            if kind not in AttributeType.values:
                errors.append(f"{at}.type: debe ser uno de {AttributeType.values}")
            _text(errors, f"{at}.unit", attribute.get("unit", ""), "attribute_unit", required=False)
            if attribute.get("unit") and kind != AttributeType.NUMBER:
                errors.append(f"{at}.unit: solo en atributos numéricos")
            choices = attribute.get("choices")
            if kind == AttributeType.CHOICE:
                if not choices or not all(isinstance(x, str) and x.strip() for x in choices):
                    errors.append(f"{at}.choices: lista de opciones obligatoria")
                elif len(set(choices)) != len(choices):
                    errors.append(f"{at}.choices: opciones repetidas")
            elif choices:
                errors.append(f"{at}.choices: solo en atributos de tipo lista")
        for p, plan in enumerate(cls.get("plans") or []):
            _validate_plan(errors, f"{where}.plans[{p}]", plan)
            plan_code = str(plan.get("code", "")).lower()
            if plan_code in plan_codes:
                errors.append(f"{where}.plans[{p}].code: {plan.get('code')} repetido")
            plan_codes.add(plan_code)
    failures = set()
    for f, failure in enumerate(data.get("failure_codes") or []):
        where = f"failure_codes[{f}]"
        if failure.get("kind") not in FAILURE_KINDS:
            errors.append(f"{where}.kind: debe ser uno de {FAILURE_KINDS}")
        _text(errors, f"{where}.code", failure.get("code"), "failure_code")
        _text(errors, f"{where}.name", failure.get("name"), "failure_name")
        key = (failure.get("kind"), str(failure.get("code", "")).upper())
        if key in failures:
            errors.append(f"{where}: {key} repetido")
        failures.add(key)
    if not data.get("asset_classes"):
        errors.append("asset_classes: al menos una clase")
    return errors
