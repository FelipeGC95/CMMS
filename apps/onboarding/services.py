"""Asistente de puesta en marcha (ORG-03): cada paso reutiliza el servicio que ya existe.

rubro → `apply_industry_template(parts=("classes", "failure_codes"))`; sitios →
`organizations.services.create_location`; activos → `assets.services.create_asset`
(o la importación Excel); planes sugeridos → `apply_industry_template(parts=("plans",),
plan_codes=...)` + `assign_plan_to_class`; lecturas → `record_meter_reading`;
calendario → `maintenance.selectors.plan_calendar`.

Cada paso es idempotente: repetirlo no duplica ubicaciones, activos, planes,
asignaciones ni lecturas. Todo paso se puede saltar y retomar (CA2).
"""

import datetime
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.text import slugify
from django.utils.translation import gettext as _

from apps.accounts.permissions import Perm, require_perm
from apps.assets.models import Asset, Meter
from apps.assets.services import create_asset, record_meter_reading
from apps.core.tenancy import tenant_context
from apps.maintenance.industry_templates import apply_industry_template, preview_industry_template
from apps.maintenance.models import MaintenancePlan, PlanAssignment
from apps.maintenance.services import assign_plan, assign_plan_to_class
from apps.maintenance.templates_library import INDUSTRIES
from apps.organizations.models import Location
from apps.organizations.services import create_location

from .models import STEPS, OnboardingState, Step

MAX_AREAS = 10


def _state(membership) -> OnboardingState:
    require_perm(membership, Perm.ORGANIZATION_MANAGE)
    state, _created = OnboardingState.objects.get_or_create(organization=membership.organization)
    return state


def create_for_new_organization(*, organization) -> OnboardingState:
    """Al confirmar el autoregistro: el asistente queda pendiente y el inicio lleva a él."""
    with tenant_context(organization):
        state, _created = OnboardingState.objects.get_or_create(organization=organization)
    return state


@transaction.atomic
def open_wizard(*, membership) -> OnboardingState:
    """Abre (o retoma) el asistente. Desde aquí el inicio ya no redirige, solo muestra el aviso."""
    state = _state(membership)
    if state.started_at is None:
        state.started_at = timezone.now()
        state.save(update_fields=["started_at", "updated_at"])
    return state


def _mark(state, step, *, skipped: bool) -> OnboardingState:
    done, other = (
        (state.skipped_steps, state.completed_steps)
        if skipped
        else (state.completed_steps, state.skipped_steps)
    )
    if step not in done:
        done.append(step)
    if step in other:
        other.remove(step)
    index = STEPS.index(step)
    if index + 1 < len(STEPS):
        state.current_step = STEPS[index + 1]
    else:
        state.current_step = step
        state.finished_at = state.finished_at or timezone.now()
    state.save()
    return state


@transaction.atomic
def skip_step(*, membership, step: str) -> OnboardingState:
    if step not in STEPS:
        raise ValidationError(_("Paso desconocido."))
    return _mark(_state(membership), step, skipped=True)


@transaction.atomic
def dismiss(*, membership) -> OnboardingState:
    state = _state(membership)
    state.dismissed_at = timezone.now()
    state.save(update_fields=["dismissed_at", "updated_at"])
    return state


# --- 1. Rubro --------------------------------------------------------------------


@transaction.atomic
def choose_industry(*, membership, industry: str):
    """Copia clases, atributos y catálogo de fallas del rubro (CA3). Los planes, en el paso 4."""
    if industry not in INDUSTRIES:
        raise ValidationError(_("Elige un rubro de la lista."))
    state = _state(membership)
    summary = apply_industry_template(
        organization=membership.organization,
        industry=industry,
        by=membership.user,
        parts=("classes", "failure_codes"),
    )
    state.industry = industry
    _mark(state, Step.INDUSTRY, skipped=False)
    return summary


# --- 2. Sitios ---------------------------------------------------------------------


def _code(text: str, taken: set[str], *, prefix: str = "") -> str:
    base = (prefix + slugify(text).upper())[:30].strip("-") or "UBIC"
    code, n = base, 2
    while code.lower() in taken:
        suffix = f"-{n}"
        code, n = base[: 30 - len(suffix)] + suffix, n + 1
    taken.add(code.lower())
    return code


@transaction.atomic
def create_locations(*, membership, site_name: str, areas=()) -> list[Location]:
    """Un sitio y sus áreas con códigos automáticos. Repetirlo reutiliza los que ya existen (por nombre)."""
    state = _state(membership)
    site_name = site_name.strip()
    areas = [a.strip() for a in areas if a.strip()][:MAX_AREAS]
    if not site_name:
        raise ValidationError(_("Escribe el nombre del sitio."))
    taken = {c.lower() for c in Location.objects.values_list("code", flat=True)}
    site = Location.objects.filter(parent=None, name__iexact=site_name).first()
    if site is None:
        site = create_location(
            membership=membership, code=_code(site_name, taken), name=site_name, kind=Location.Kind.SITE
        )
    created = [site]
    existing = {name.lower() for name in site.children.values_list("name", flat=True)}
    for area in areas:
        if area.lower() in existing:
            continue
        existing.add(area.lower())
        created.append(
            create_location(
                membership=membership,
                code=_code(area, taken, prefix=f"{site.code}-"),
                name=area,
                kind=Location.Kind.AREA,
                parent=site,
            )
        )
    _mark(state, Step.LOCATIONS, skipped=False)
    return created


# --- 3. Activos (alta rápida; la importación Excel usa su propia pantalla) ---------


def _asset_code(asset_class, taken: set[str]) -> str:
    n = 1
    while True:
        code = f"{asset_class.code}-{n:02d}"[:30]
        if code.lower() not in taken:
            taken.add(code.lower())
            return code
        n += 1


@transaction.atomic
def quick_add_assets(*, membership, rows) -> tuple[list[Asset], list[str]]:
    """Alta rápida: nombre, clase, ubicación y medidor opcionales; el código se genera.

    `rows`: dicts con `name`, `asset_class`, `location` y `meter_kind`. Una fila
    cuyo nombre ya existe en la misma clase se omite (repetir el envío no duplica).
    """
    _state(membership)
    taken = {c.lower() for c in Asset.objects.values_list("code", flat=True)}
    existing = {(n.lower(), c) for n, c in Asset.objects.values_list("name", "asset_class_id")}
    created, skipped = [], []
    for row in rows:
        name, asset_class = row["name"].strip(), row["asset_class"]
        if not name or asset_class is None:
            continue
        if (name.lower(), asset_class.pk) in existing:
            skipped.append(name)
            continue
        existing.add((name.lower(), asset_class.pk))
        asset = create_asset(
            membership=membership,
            code=_asset_code(asset_class, taken),
            name=name,
            asset_class=asset_class,
            location=row.get("location"),
        )
        if row.get("meter_kind"):
            Meter.objects.get_or_create(asset=asset, kind=row["meter_kind"], name="")
        created.append(asset)
    return created, skipped


@transaction.atomic
def finish_assets(*, membership) -> OnboardingState:
    return _mark(_state(membership), Step.ASSETS, skipped=False)


# --- 4. Planes sugeridos por clase -----------------------------------------------


def suggested_plans(*, membership) -> list[dict]:
    """Planes del rubro elegido para las clases que tienen activos, con cuántos activos recibirían cada uno.

    Los que la organización ya tiene (por código, p. ej. al repetir el paso) vienen
    con `exists=True`: no se vuelven a crear, pero sí se asignan a los activos nuevos.
    """
    state = _state(membership)
    if not state.industry:
        return []
    counts = {}
    for code in (
        Asset.objects.filter(is_active=True)
        .exclude(status="retired")
        .values_list("asset_class__code", flat=True)
    ):
        counts[code.lower()] = counts.get(code.lower(), 0) + 1
    summary = preview_industry_template(
        organization=membership.organization, industry=state.industry, parts=("plans",)
    )
    rows = [
        {**plan, "asset_count": counts[plan["class_code"].lower()], "exists": False}
        for plan in summary.plans
        if plan["class_code"].lower() in counts
    ]
    existing = MaintenancePlan.objects.filter(
        code__in=summary.plans_skipped, current_version__isnull=False, asset_class__isnull=False
    ).select_related("asset_class", "current_version")
    for plan in existing:
        if plan.asset_class.code.lower() in counts:
            rows.append(
                {
                    "code": plan.code,
                    "name": plan.name,
                    "class_code": plan.asset_class.code,
                    "class_name": plan.asset_class.name,
                    "frequency_label": str(plan.current_version.frequency_label),
                    "asset_count": counts[plan.asset_class.code.lower()],
                    "exists": True,
                }
            )
    return rows


def _ensure_meters(plans):
    """Un plan por uso necesita el medidor en cada activo de su clase: se crea si falta."""
    for plan in plans:
        version = plan.current_version
        if version is None or not version.uses_meter or plan.asset_class_id is None:
            continue
        missing = (
            Asset.objects.filter(asset_class_id=plan.asset_class_id, is_active=True)
            .exclude(status="retired")
            .exclude(pk__in=Meter.objects.filter(kind=version.meter_kind, is_active=True).values("asset_id"))
        )
        Meter.objects.bulk_create(
            [
                Meter(asset=asset, kind=version.meter_kind, organization_id=asset.organization_id)
                for asset in missing
            ]
        )


@transaction.atomic
def create_suggested_plans(*, membership, plan_codes) -> tuple[int, int, list[str]]:
    """Crea los planes marcados (publicados) y los asigna a los activos de su clase (PM-04 CA1).

    Devuelve (planes creados, asignaciones nuevas, avisos). Repetirlo no duplica
    planes ni reinicia asignaciones existentes.
    """
    state = _state(membership)
    rows = suggested_plans(membership=membership)
    allowed = {row["code"].lower(): row for row in rows}
    chosen = [allowed[c.lower()] for c in plan_codes if c.lower() in allowed]
    codes = [row["code"] for row in chosen]
    created = 0
    if any(not row["exists"] for row in chosen):
        summary = apply_industry_template(
            organization=membership.organization,
            industry=state.industry,
            by=membership.user,
            parts=("plans",),
            plan_codes=set(codes),
        )
        created = len(summary.plans)
    plans = list(
        MaintenancePlan.objects.filter(
            code__in=codes, is_active=True, asset_class__isnull=False
        ).select_related("current_version")
    )
    _ensure_meters(plans)
    assigned, warnings = 0, []
    for plan in plans:
        n, plan_warnings = assign_plan_to_class(membership=membership, plan=plan)
        assigned += n
        warnings += plan_warnings
    _mark(state, Step.PLANS, skipped=False)
    return created, assigned, warnings


# --- 5. Lecturas iniciales ---------------------------------------------------------


def meters_for_readings():
    """Medidores que usan los planes por uso asignados: a estos se les pide la lectura actual."""
    return (
        Meter.objects.filter(
            is_active=True,
            pk__in=PlanAssignment.objects.filter(is_active=True, meter__isnull=False).values("meter_id"),
        )
        .select_related("asset")
        .order_by("asset__code", "kind")
    )


@transaction.atomic
def record_initial_readings(*, membership, values: dict) -> int:
    """Registra la lectura actual de cada medidor (`values`: {meter_id: Decimal}; vacío = sin lectura).

    Si era la primera lectura del medidor, sus planes por uso se recalculan desde
    ella (lectura + intervalo), para no quedar vencidos contra un medidor en 0.
    Repetir el mismo valor no crea otra lectura.
    """
    state = _state(membership)
    recorded = 0
    meters = {m.pk: m for m in meters_for_readings()}
    for meter_id, value in values.items():
        meter = meters.get(meter_id)
        if meter is None or value is None:
            continue
        value = Decimal(value)
        if meter.current_value is not None and meter.current_value == value:
            continue
        first = meter.current_value is None
        record_meter_reading(meter=meter, value=value, membership=membership, generate_work_orders=False)
        recorded += 1
        if first:
            for assignment in PlanAssignment.objects.filter(meter=meter, is_active=True).select_related(
                "plan__current_version", "asset"
            ):
                assign_plan(
                    plan=assignment.plan,
                    asset=assignment.asset,
                    meter=meter,
                    starts_on=assignment.next_due_on,
                    by=membership.user,
                )
    _mark(state, Step.READINGS, skipped=False)
    return recorded


# --- 6. Resumen del calendario -----------------------------------------------------


def calendar_summary(*, membership, today: datetime.date | None = None) -> dict:
    """Preventivos de los próximos 30 y 90 días según la proyección de PM-06."""
    from apps.maintenance.selectors import plan_calendar

    today = today or timezone.localdate()
    calendar = plan_calendar(
        membership=membership, start=today, end=today + datetime.timedelta(days=90), today=today
    )
    due = [o for o in calendar.occurrences if o.absorbed_into is None]
    limit30 = today + datetime.timedelta(days=30)
    in30 = [o for o in due if o.date <= limit30]
    return {
        "today": today,
        "count_30": len(in30),
        "count_90": len(due),
        "hours_30": sum(o.estimated_minutes for o in in30) / 60,
        "hours_90": sum(o.estimated_minutes for o in due) / 60,
        "upcoming": due[:8],
        "unestimated": len(calendar.unestimated),
        "assignments": PlanAssignment.objects.filter(is_active=True).count(),
    }


@transaction.atomic
def finish(*, membership) -> OnboardingState:
    return _mark(_state(membership), Step.CALENDAR, skipped=False)
