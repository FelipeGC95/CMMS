"""Planes preventivos: versiones, asignaciones y programador (PM-01..05, ADR-016)."""

import datetime
import logging
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import F, Max, Q
from django.utils import timezone
from django.utils.translation import gettext as _

from apps.accounts.permissions import Perm, require_perm
from apps.audit import services as audit
from apps.audit.models import AuditEvent

from .models import (
    DEFAULT_HORIZON_DAYS,
    MAX_HORIZON_DAYS,
    ItemType,
    MaintenancePlan,
    MaintenanceSettings,
    PlanAbsorption,
    PlanAssignment,
    PlanResource,
    PlanTaskItem,
    PlanVersion,
)
from .scheduling import absorbs, estimated_meter_date, next_schedule
from .selectors import busy_assignment_ids

logger = logging.getLogger(__name__)

# Aviso mínimo para planes por uso: generar la OT al 90 % del intervalo.
METER_MIN_LEAD = Decimal("0.10")
# Campos de la versión que cambian cuándo vence un plan.
TRIGGER_FIELDS = ("calendar_interval", "calendar_unit", "meter_kind", "meter_interval")
ITEM_FIELDS = ("order", "text", "item_type", "unit", "min_value", "max_value", "required")
RESOURCE_FIELDS = ("kind", "description", "quantity", "unit", "part")


class PlanVersionError(ValidationError):
    pass


# --- Versiones --------------------------------------------------------------


def locked_plans():
    """Planes para bloquear con SELECT … FOR UPDATE.

    El manager por defecto hace select_related("current_version"), un LEFT JOIN
    porque la FK es nullable, y PostgreSQL no admite FOR UPDATE sobre el lado
    nullable de un outer join. Por eso se quita el join antes de bloquear
    (SQLite no bloquea filas y no reproduce el error).
    """
    return MaintenancePlan.objects.select_related(None).select_for_update()


def _add_content(version: PlanVersion, *, items=(), resources=()):
    PlanTaskItem.objects.bulk_create(
        [
            PlanTaskItem(
                organization=version.organization,
                version=version,
                **{
                    "order": i,
                    **item,
                    **({"required": True} if item.get("item_type") == ItemType.PHOTO else {}),
                },
            )
            for i, item in enumerate(items, start=1)
        ]
    )
    PlanResource.objects.bulk_create(
        [PlanResource(organization=version.organization, version=version, **res) for res in resources]
    )


@transaction.atomic
def create_plan(
    *,
    code: str,
    name: str,
    asset_class=None,
    items=(),
    resources=(),
    by=None,
    **version_fields,
) -> MaintenancePlan:
    """Crea un plan con su versión 1 ya publicada (el camino corto para planes nuevos).

    `items`: dicts con text, item_type y opcionalmente unit, min_value,
    max_value, required. `version_fields`: calendar_interval, calendar_unit, meter_kind,
    meter_interval, estimated_minutes, instructions, schedule_mode, tolerance_pct.
    """
    plan = MaintenancePlan.objects.create(code=code, name=name, asset_class=asset_class)
    audit.record(action=AuditEvent.Action.CREATE, target=plan, actor=by, changes=audit.snapshot(plan))
    version = PlanVersion.objects.create(plan=plan, number=1, **version_fields)
    _add_content(version, items=items, resources=resources)
    publish_version(version=version, by=by, change_note=_("Versión inicial"))
    plan.refresh_from_db()
    return plan


@transaction.atomic
def bulk_create_published_plans(*, organization, plans, by=None, change_note: str) -> list[MaintenancePlan]:
    """Varios planes nuevos con su versión 1 ya publicada, en un número fijo de consultas.

    La misma regla que `create_plan` (frecuencia válida, checklist no vacío,
    foto siempre obligatoria), por lotes: la usan las plantillas de rubro
    (PM-07) y la importación desde Excel (PM-08). `plans`: dicts con code,
    name, asset_class, fields (campos de la versión), items (campos de
    PlanTaskItem sin `order`) y resources. Los códigos deben ser nuevos (lo
    revisa quien llama). No audita: cada llamador registra su evento.
    """
    now = timezone.now()
    records, versions, items, resources = [], [], [], []
    for data in plans:
        plan = MaintenancePlan(
            organization=organization,
            code=data["code"],
            name=data["name"],
            asset_class=data.get("asset_class"),
        )
        version = PlanVersion(
            organization=organization,
            plan=plan,
            number=1,
            status=PlanVersion.Status.PUBLISHED,
            published_at=now,
            published_by=by,
            change_note=change_note[:300],
            **data["fields"],
        )
        version.clean()
        if not data["items"]:
            raise PlanVersionError(_("Agrega al menos un ítem al checklist antes de publicar."))
        records.append(plan)
        versions.append(version)
        items += [
            PlanTaskItem(
                organization=organization,
                version=version,
                **{
                    "order": i,
                    **item,
                    **({"required": True} if item.get("item_type") == ItemType.PHOTO else {}),
                },
            )
            for i, item in enumerate(data["items"], start=1)
        ]
        resources += [
            PlanResource(organization=organization, version=version, **r) for r in data["resources"]
        ]
    # Versiones inmutables (ADR-016): se crean ya publicadas; nunca se actualizan por lotes.
    MaintenancePlan.objects.bulk_create(records)
    PlanVersion.objects.bulk_create(versions)
    PlanTaskItem.objects.bulk_create(items)
    PlanResource.objects.bulk_create(resources)
    for plan, version in zip(records, versions, strict=True):
        plan.current_version = version
    MaintenancePlan.objects.bulk_update(records, ["current_version"])
    return records


@transaction.atomic
def create_draft_with_content(
    *, plan: MaintenancePlan, fields: dict, items, resources, by=None
) -> PlanVersion:
    """Nueva versión borrador de un plan existente con el contenido dado (PM-08).

    Nunca toca la versión publicada: el borrador se revisa y publica aparte.
    Falla si el plan ya tiene un borrador.
    """
    if plan.versions.filter(status=PlanVersion.Status.DRAFT).exists():
        raise PlanVersionError(_("«%(plan)s» ya tiene un borrador.") % {"plan": plan.code})
    number = (plan.versions.aggregate(n=Max("number"))["n"] or 0) + 1
    draft = PlanVersion.objects.create(plan=plan, number=number, **fields)
    _add_content(draft, items=items, resources=resources)
    audit.record(
        action=AuditEvent.Action.CREATE, target=draft, actor=by, changes=audit.snapshot(draft, DRAFT_FIELDS)
    )
    return draft


@transaction.atomic
def start_draft(*, plan: MaintenancePlan) -> PlanVersion:
    """Devuelve el borrador del plan; si no hay, lo crea copiando la versión vigente."""
    draft = plan.versions.filter(status=PlanVersion.Status.DRAFT).first()
    if draft:
        return draft
    source = plan.current_version
    number = (plan.versions.aggregate(n=Max("number"))["n"] or 0) + 1
    copied = {}
    if source is not None:
        copied = {f: getattr(source, f) for f in PlanVersion.CONTENT_FIELDS if f not in ("plan_id", "number")}
    draft = PlanVersion.objects.create(plan=plan, number=number, **copied)
    if source is not None:
        _add_content(
            draft,
            items=[{f: getattr(i, f) for f in ITEM_FIELDS if f != "order"} for i in source.items.all()],
            resources=[
                {f: getattr(r, f) for f in RESOURCE_FIELDS} for r in source.resources.select_related("part")
            ],
        )
    return draft


@transaction.atomic
def publish_version(*, version: PlanVersion, by=None, change_note: str = "") -> PlanVersion:
    """Publica un borrador: pasa a ser la versión vigente y la anterior queda reemplazada.

    Si cambió la frecuencia, recalcula en la misma transacción los vencimientos
    de las asignaciones sin OT abierta (las OT futuras usan la nueva versión).
    """
    version = PlanVersion.objects.select_for_update().get(pk=version.pk)
    if not version.is_draft:
        raise PlanVersionError(_("La versión %(number)s ya está publicada.") % {"number": version.number})
    version.clean()
    if not version.items.exists():
        raise PlanVersionError(_("Agrega al menos un ítem al checklist antes de publicar."))

    plan = locked_plans().get(pk=version.plan_id)
    previous = plan.current_version
    if previous is not None:
        previous.status = PlanVersion.Status.SUPERSEDED
        previous.save(update_fields=["status", "updated_at"])
    version.status = PlanVersion.Status.PUBLISHED
    version.published_at = timezone.now()
    version.published_by = by
    version.change_note = change_note[:300] or version.change_note
    version.save(update_fields=["status", "published_at", "published_by", "change_note", "updated_at"])
    plan.current_version = version
    plan.save(update_fields=["current_version", "updated_at"])
    audit.record(
        action=AuditEvent.Action.TRANSITION,
        target=version,
        actor=by,
        changes={
            "status": [PlanVersion.Status.DRAFT, PlanVersion.Status.PUBLISHED],
            "replaces": previous.number if previous is not None else None,
            "change_note": version.change_note,
        },
    )

    if previous is not None and any(getattr(previous, f) != getattr(version, f) for f in TRIGGER_FIELDS):
        reschedule_after_trigger_change(plan=plan, previous=previous, current=version)
    return version


def reschedule_after_trigger_change(*, plan, previous: PlanVersion, current: PlanVersion) -> int:
    """Recalcula vencimientos con la nueva frecuencia desde la última ejecución (o lectura).

    Las asignaciones con una OT abierta (propia o de un plan que las absorbió)
    no se tocan: esa OT ya está en curso y al cerrarla se reprograma con la
    versión vigente.
    """
    open_ids = busy_assignment_ids(plan.assignments.all())
    today = timezone.localdate()
    changed = 0
    for assignment in plan.assignments.filter(is_active=True).select_related("asset", "meter"):
        if assignment.id in open_ids:
            continue
        # Calendario: desde la última ejecución; si nunca se ejecutó, desde el
        # inicio implícito del ciclo anterior (vencimiento − frecuencia anterior).
        base_date = assignment.last_done_on
        if base_date is None and assignment.next_due_on and previous.calendar_interval:
            base_date = previous.shift(assignment.next_due_on, -1)
        assignment.next_due_on = current.shift(base_date or today) if current.calendar_interval else None
        assignment.anchor_day = assignment.next_due_on.day if assignment.next_due_on else None
        # Uso: igual, con la última lectura de cierre o el inicio del ciclo anterior.
        if current.uses_meter:
            meter = assignment.meter
            if meter is None or meter.kind != current.meter_kind:
                meter = assignment.asset.meters.filter(kind=current.meter_kind, is_active=True).first()
            base_value = assignment.last_done_meter_value
            if base_value is None and assignment.next_due_meter_value is not None and previous.uses_meter:
                base_value = assignment.next_due_meter_value - previous.meter_interval
            if base_value is None and meter is not None:
                base_value = meter.current_value or Decimal(0)
            assignment.meter = meter
            assignment.next_due_meter_value = (
                base_value + current.meter_interval if meter is not None and base_value is not None else None
            )
        else:
            assignment.meter, assignment.next_due_meter_value = None, None
        assignment.save(
            update_fields=["next_due_on", "anchor_day", "meter", "next_due_meter_value", "updated_at"]
        )
        changed += 1
    return changed


# --- Asignaciones -----------------------------------------------------------


@transaction.atomic
def assign_plan(
    *,
    plan: MaintenancePlan,
    asset,
    starts_on: datetime.date | None = None,
    meter=None,
    next_due_meter_value=None,
    by=None,
) -> PlanAssignment:
    """Aplica un plan a un activo y calcula su primer vencimiento con la versión vigente.

    Por calendario, el primer vencimiento es `starts_on` (por defecto, hoy +
    frecuencia). Por uso, es la lectura actual + intervalo.
    """
    version = plan.current_version
    if version is None:
        raise PlanVersionError(_("«%(plan)s» no tiene una versión publicada.") % {"plan": plan})
    if asset.status == "retired":  # dado de baja: ya no recibe preventivos (AST-04 CA3)
        raise ValidationError(_("%(asset)s está dado de baja.") % {"asset": asset.code})
    defaults = {
        "is_active": True,
        "next_due_on": None,
        "anchor_day": None,
        "meter": None,
        "next_due_meter_value": None,
    }
    if version.calendar_interval:
        defaults["next_due_on"] = starts_on or version.shift(timezone.localdate())
        defaults["anchor_day"] = defaults["next_due_on"].day
    if version.uses_meter:
        meter = meter or asset.meters.filter(kind=version.meter_kind, is_active=True).first()
        if meter is None:
            raise ValidationError(
                _("%(asset)s no tiene un medidor de tipo «%(kind)s».")
                % {"asset": asset.code, "kind": version.get_meter_kind_display()}
            )
        defaults["meter"] = meter
        defaults["next_due_meter_value"] = (
            next_due_meter_value
            if next_due_meter_value is not None
            else (meter.current_value or Decimal(0)) + version.meter_interval
        )
    before = PlanAssignment.objects.filter(plan=plan, asset=asset).first()
    before = audit.snapshot(before) if before is not None else {}
    assignment, created = PlanAssignment.objects.update_or_create(plan=plan, asset=asset, defaults=defaults)
    changes = audit.diff(before, audit.snapshot(assignment))
    if changes:
        action = AuditEvent.Action.CREATE if created else AuditEvent.Action.UPDATE
        audit.record(action=action, target=assignment, actor=by, changes=changes)
    return assignment


@transaction.atomic
def deactivate_asset_assignments(*, asset, by=None, reason: str = "") -> int:
    """Desactiva las asignaciones de plan del activo (al darlo de baja, AST-04 CA3).

    No se borran: conservan su historial (última ejecución) y quedan auditadas.
    Las OT preventivas ya generadas no se tocan.
    """
    assignments = list(PlanAssignment.objects.select_for_update().filter(asset=asset, is_active=True))
    for assignment in assignments:
        assignment.is_active = False
        assignment.save(update_fields=["is_active", "updated_at"])
        audit.record(
            action=AuditEvent.Action.UPDATE,
            target=assignment,
            actor=by,
            changes={"is_active": [True, False], "reason": reason[:200]},
        )
    return len(assignments)


@transaction.atomic
def reactivate_assignments(*, asset, assignments, by=None) -> list[PlanAssignment]:
    """Vuelve a activar las asignaciones elegidas al reactivar un activo (AST-04).

    Reutiliza `assign_plan`: el próximo vencimiento se recalcula desde hoy con la
    versión vigente (calendario: hoy + frecuencia; uso: lectura actual +
    intervalo) y el cambio queda auditado. Conserva el medidor elegido si sigue
    activo y es del tipo del plan.
    """
    reactivated = []
    for assignment in assignments:
        plan, meter = assignment.plan, assignment.meter
        if meter is not None and (not meter.is_active or meter.kind != plan.meter_kind):
            meter = None
        reactivated.append(assign_plan(plan=plan, asset=asset, meter=meter, by=by))
    return reactivated


# --- Programador ------------------------------------------------------------


def _meter_due(assignment: PlanAssignment, version: PlanVersion, horizon_days: int) -> bool:
    meter, target = assignment.meter, assignment.next_due_meter_value
    if meter is None or target is None or meter.current_value is None or not version.uses_meter:
        return False
    lead = max(
        (meter.avg_daily_usage or Decimal(0)) * horizon_days,
        version.meter_interval * METER_MIN_LEAD,
    )
    return meter.current_value + lead >= target


def _estimated_meter_date(assignment: PlanAssignment, today: datetime.date) -> datetime.date:
    """Fecha estimada en que el medidor llegará al vencimiento (según uso promedio; sin él, hoy)."""
    meter = assignment.meter
    return (
        estimated_meter_date(
            current=meter.current_value,
            target=assignment.next_due_meter_value,
            avg_daily_usage=meter.avg_daily_usage,
            today=today,
        )
        or today
    )


def _active_assignments():
    return (
        PlanAssignment.objects.filter(
            is_active=True,
            plan__is_active=True,
            plan__current_version__isnull=False,
            asset__is_active=True,
        )
        .exclude(asset__status="retired")
        .select_related("plan__current_version", "asset", "meter")
    )


def absorption_due(*, main: PlanAssignment, due_on: datetime.date | None, other: PlanAssignment) -> bool:
    """¿El plan menor (`other`) vence junto al mayor (`main`) en el mismo activo? (PM-05 CA3)

    Regla en `scheduling.absorbs`, compartida con la proyección del calendario (PM-06).
    """
    return absorbs(
        version=main.plan.current_version,
        minor_version=other.plan.current_version,
        due_on=due_on,
        meter_target=main.next_due_meter_value,
        meter_id=main.meter_id,
        other_due_on=other.next_due_on,
        other_meter_target=other.next_due_meter_value,
        other_meter_id=other.meter_id,
    )


def generate_due_work_orders(*, today: datetime.date | None = None, horizon_days=None, meter=None):
    """Genera OT preventivas que vencen por fecha o por uso, lo que ocurra primero (PM-02, PM-05).

    Usa la versión vigente del plan: la OT guarda esa versión y copia su
    checklist. Idempotente: una asignación con OT abierta (propia o de un plan
    que la absorbió) no genera otra; además lo garantiza un índice único
    parcial en WorkOrder. Con `meter`, revisa solo los planes de ese medidor
    (tras registrar una lectura).

    Absorción (PM-05 CA3): los planes que absorben a otros se revisan primero;
    si en el mismo activo un plan absorbido vence junto al mayor
    (`absorption_due`), la OT del mayor lleva los dos checklists en secciones
    rotuladas y el menor no genera OT propia.
    """
    from apps.work.models import WorkOrder, WorkOrderAbsorption
    from apps.work.services import create_work_order

    from .selectors import maintenance_settings

    today = today or timezone.localdate()
    if horizon_days is None:  # el de la organización (PM-05 CA1: configurable, 14 días por defecto)
        horizon_days = maintenance_settings().horizon_days
    limit = today + datetime.timedelta(days=horizon_days)
    candidates = _active_assignments().filter(
        Q(next_due_on__lte=limit) | Q(meter__isnull=False, next_due_meter_value__isnull=False)
    )
    if meter is not None:
        candidates = candidates.filter(meter=meter)
    candidates = list(candidates)
    busy = busy_assignment_ids(candidates)

    due = []
    for assignment in candidates:
        if assignment.id in busy:
            continue
        version = assignment.plan.current_version
        by_date = assignment.next_due_on is not None and assignment.next_due_on <= limit
        by_meter = _meter_due(assignment, version, horizon_days)
        if not (by_date or by_meter):
            continue
        due_dates = [d for d in [assignment.next_due_on] if d is not None]
        if by_meter:
            due_dates.append(_estimated_meter_date(assignment, today))
        due.append((assignment, min(due_dates)))

    # Absorciones de los planes que vencen: una consulta para las reglas y otra para
    # las asignaciones menores de esos mismos activos (sin consultas por fila).
    absorbs = {}
    for plan_id, absorbed_id in PlanAbsorption.objects.filter(
        plan_id__in={a.plan_id for a, _due in due}, absorbed_plan__is_active=True
    ).values_list("plan_id", "absorbed_plan_id"):
        absorbs.setdefault(plan_id, set()).add(absorbed_id)
    minors = {}
    if absorbs:
        others = list(
            _active_assignments().filter(
                asset_id__in={a.asset_id for a, _due in due if a.plan_id in absorbs},
                plan_id__in=set().union(*absorbs.values()),
            )
        )
        busy |= busy_assignment_ids(others)
        for other in others:
            minors.setdefault(other.asset_id, []).append(other)
    due.sort(key=lambda row: row[0].plan_id not in absorbs)  # primero los que absorben
    planned_parts = _planned_parts(due, minors)

    created = []
    for assignment, due_on in due:
        if assignment.id in busy:
            continue
        version = assignment.plan.current_version
        absorbed = [
            other
            for other in minors.get(assignment.asset_id, [])
            if other.plan_id in absorbs.get(assignment.plan_id, ())
            and other.id not in busy
            and absorption_due(main=assignment, due_on=due_on, other=other)
        ]
        with transaction.atomic():
            fields = {}
            if absorbed:
                fields["checklist_sections"] = [(assignment.plan.name, list(version.items.all()))] + [
                    (other.plan.name, list(other.plan.current_version.items.all())) for other in absorbed
                ]
                fields["description"] = _("Incluye: %(plans)s.") % {
                    "plans": ", ".join(other.plan.name for other in absorbed)
                }
            else:
                fields["checklist"] = list(version.items.all())
            work_order = create_work_order(
                work_type=WorkOrder.Type.PREVENTIVE,
                title=assignment.plan.name,
                asset=assignment.asset,
                due_on=due_on,
                due_meter_value=assignment.next_due_meter_value if assignment.meter_id else None,
                estimated_minutes=version.estimated_minutes
                + sum(other.plan.current_version.estimated_minutes for other in absorbed),
                plan_assignment=assignment,
                plan_version=version,
                status=WorkOrder.Status.PLANNED,
                **fields,
            )
            WorkOrderAbsorption.objects.bulk_create(
                [
                    WorkOrderAbsorption(
                        organization=work_order.organization,
                        work_order=work_order,
                        plan_assignment=other,
                        plan_version=other.plan.current_version,
                    )
                    for other in absorbed
                ]
            )
            if planned_parts is not None:
                _reserve_planned_parts(
                    work_order,
                    [version.pk] + [other.plan.current_version_id for other in absorbed],
                    planned_parts,
                )
        busy.add(assignment.id)
        busy.update(other.id for other in absorbed)
        created.append(work_order)
    return created


def _planned_parts(due, minors) -> dict | None:
    """Repuestos del catálogo previstos por versión vigente, en una consulta (INV-10 CA1).

    `None` si el plan de la organización no incluye inventario (no se reserva).
    """
    from apps.core.tenancy import get_current_organization
    from apps.organizations.modules import has_module

    if not due or not has_module(get_current_organization(), "inventory"):
        return None
    version_ids = {a.plan.current_version_id for a, _due in due}
    version_ids |= {o.plan.current_version_id for others in minors.values() for o in others}
    planned = {}
    for resource in PlanResource.objects.filter(
        version_id__in=version_ids, kind=PlanResource.Kind.PART, part__isnull=False, part__is_active=True
    ).order_by("created_at"):
        planned.setdefault(resource.version_id, []).append(resource)
    return planned


def _reserve_planned_parts(work_order, version_ids, planned: dict) -> None:
    """Reserva para la OT los repuestos previstos por sus versiones (principal y absorbidas).

    Repetidos se suman en una sola reserva por repuesto (enlazada al primer
    recurso). Nunca bloquea: la reserva no exige stock, y si algo falla (ej.
    bodega desactivada) la OT se crea igual sin esa reserva.
    """
    from apps.inventory.selectors import warehouse_for
    from apps.inventory.services import reserve_for_work_order

    totals = {}
    for version_id in version_ids:
        for resource in planned.get(version_id, ()):
            first, quantity = totals.get(resource.part_id, (resource, Decimal(0)))
            totals[resource.part_id] = (first, quantity + resource.quantity)
    if not totals:
        return
    warehouse = warehouse_for(work_order=work_order)
    for part_id, (resource, quantity) in totals.items():
        try:
            with transaction.atomic():
                reserve_for_work_order(
                    work_order=work_order,
                    part=part_id,
                    quantity=quantity,
                    warehouse=warehouse,
                    source="plan",
                    plan_resource=resource,
                )
        except ValidationError:
            logger.warning("No se pudo reservar el repuesto %s para la OT %s", part_id, work_order.pk)


def register_completion(
    *, assignment: PlanAssignment, completed_on: datetime.date, meter_value=None
) -> PlanAssignment:
    """Reprograma al cerrar la OT con la versión vigente del plan.

    Flotante: desde la ejecución real (fecha y lectura). Fijo: desde lo
    programado, sin quedar en el pasado.
    """
    version = assignment.plan.current_version
    assignment.last_done_on = completed_on
    base = None
    if version.uses_meter and assignment.meter_id:
        base = meter_value if meter_value is not None else assignment.meter.current_value
        if base is not None:
            assignment.last_done_meter_value = base
    assignment.next_due_on, assignment.next_due_meter_value = next_schedule(
        version=version,
        next_due_on=assignment.next_due_on,
        next_due_meter_value=assignment.next_due_meter_value,
        completed_on=completed_on,
        meter_value=base,
        anchor_day=assignment.anchor_day,
    )
    assignment.save(
        update_fields=[
            "last_done_on",
            "next_due_on",
            "last_done_meter_value",
            "next_due_meter_value",
            "updated_at",
        ]
    )
    return assignment


def shift_meter_targets(*, meter, delta: Decimal) -> None:
    """Tras reiniciar o cambiar un medidor, conserva lo que faltaba para cada vencimiento."""
    PlanAssignment.objects.filter(meter=meter, next_due_meter_value__isnull=False).update(
        next_due_meter_value=F("next_due_meter_value") + delta
    )
    PlanAssignment.objects.filter(meter=meter, last_done_meter_value__isnull=False).update(
        last_done_meter_value=F("last_done_meter_value") + delta
    )


# --- Edición desde la web (planificador) -------------------------------------
# Toda escritura de las pantallas pasa por aquí: exige `plan.manage`, respeta el
# alcance por sitio al asignar y deja auditoría. La semántica del programador
# (vencimientos, publicación) sigue siendo la de las funciones de arriba.

PLAN_FIELDS = ("code", "name", "asset_class", "is_active")
DRAFT_FIELDS = (
    "estimated_minutes", "instructions", "calendar_interval", "calendar_unit", "meter_kind",
    "meter_interval", "schedule_mode", "tolerance_pct",
)  # fmt: skip


def _require_plan_manager(membership):
    require_perm(membership, Perm.PLAN_MANAGE)


def _check_draft_version(version: PlanVersion) -> None:
    if not version.is_draft:
        raise PlanVersionError(
            _("La versión %(number)s está publicada: crea un borrador para cambiarla.")
            % {"number": version.number}
        )


def _clean_measurement(item_type, unit, min_value, max_value):
    """Unidad y rango solo aplican a mediciones; en otros tipos se descartan."""
    if item_type not in ItemType.values:
        raise ValidationError(_("Tipo de ítem no válido."))
    if item_type != ItemType.MEASUREMENT:
        return "", None, None
    if min_value is not None and max_value is not None and min_value > max_value:
        raise ValidationError(_("El máximo no puede ser menor que el mínimo."))
    return unit.strip(), min_value, max_value


@transaction.atomic
def create_plan_with_draft(*, membership, code: str, name: str, asset_class=None, **version_fields):
    """Crea un plan nuevo con su versión 1 en borrador (se publica al tener checklist)."""
    _require_plan_manager(membership)
    code, name = code.strip(), name.strip()
    if MaintenancePlan.objects.filter(code__iexact=code).exists():
        raise ValidationError({"code": _("Ya existe un plan con ese código.")})
    plan = MaintenancePlan.objects.create(code=code, name=name, asset_class=asset_class)
    audit.record(
        action=AuditEvent.Action.CREATE, target=plan, membership=membership, changes=audit.snapshot(plan)
    )
    version = PlanVersion.objects.create(plan=plan, number=1, **version_fields)
    audit.record(
        action=AuditEvent.Action.CREATE,
        target=version,
        membership=membership,
        changes=audit.snapshot(version, DRAFT_FIELDS),
    )
    return plan


@transaction.atomic
def update_plan(*, membership, plan: MaintenancePlan, code: str, name: str, asset_class, is_active: bool):
    """Datos del plan que no se versionan: código, nombre, clase y si está activo."""
    _require_plan_manager(membership)
    code = code.strip()
    if MaintenancePlan.objects.filter(code__iexact=code).exclude(pk=plan.pk).exists():
        raise ValidationError({"code": _("Ya existe un plan con ese código.")})
    before = audit.snapshot(plan, PLAN_FIELDS)
    plan.code, plan.name, plan.asset_class, plan.is_active = code, name.strip(), asset_class, is_active
    plan.save(update_fields=["code", "name", "asset_class", "is_active", "updated_at"])
    changes = audit.diff(before, audit.snapshot(plan, PLAN_FIELDS))
    if changes:
        audit.record(action=AuditEvent.Action.UPDATE, target=plan, membership=membership, changes=changes)
    return plan


@transaction.atomic
def create_draft(*, membership, plan: MaintenancePlan) -> PlanVersion:
    """Borrador copiado de la versión vigente; si ya existe, devuelve el mismo (idempotente)."""
    _require_plan_manager(membership)
    locked_plans().get(pk=plan.pk)
    existed = plan.versions.filter(status=PlanVersion.Status.DRAFT).exists()
    draft = start_draft(plan=plan)
    if not existed:
        source = plan.current_version
        audit.record(
            action=AuditEvent.Action.CREATE,
            target=draft,
            membership=membership,
            changes={"number": draft.number, "copied_from": source.number if source else None},
        )
    return draft


@transaction.atomic
def update_draft(*, membership, version: PlanVersion, **fields) -> PlanVersion:
    """Frecuencia, reprogramación, tolerancia, duración e instrucciones del borrador."""
    _require_plan_manager(membership)
    _check_draft_version(version)
    unknown = set(fields) - set(DRAFT_FIELDS)
    if unknown:
        raise ValueError(f"Campos no editables: {sorted(unknown)}")
    before = audit.snapshot(version, DRAFT_FIELDS)
    for key, value in fields.items():
        setattr(version, key, value)
    if not version.meter_kind:
        version.meter_interval = None
    version.clean()
    version.save()
    changes = audit.diff(before, audit.snapshot(version, DRAFT_FIELDS))
    if changes:
        audit.record(action=AuditEvent.Action.UPDATE, target=version, membership=membership, changes=changes)
    return version


@transaction.atomic
def add_draft_item(
    *, membership, version, text, item_type, unit="", min_value=None, max_value=None, required=True
) -> PlanTaskItem:
    _require_plan_manager(membership)
    _check_draft_version(version)
    unit, min_value, max_value = _clean_measurement(item_type, unit, min_value, max_value)
    required = required or item_type == ItemType.PHOTO  # «foto obligatoria» siempre bloquea el cierre
    last = version.items.aggregate(n=Max("order"))["n"] or 0
    item = PlanTaskItem.objects.create(
        version=version,
        order=last + 1,
        text=text.strip(),
        item_type=item_type,
        unit=unit,
        min_value=min_value,
        max_value=max_value,
        required=required,
    )
    audit.record(
        action=AuditEvent.Action.CREATE,
        target=item,
        membership=membership,
        changes=audit.snapshot(item, ITEM_FIELDS),
    )
    return item


@transaction.atomic
def update_draft_item(
    *, membership, item, text, item_type, unit="", min_value=None, max_value=None, required=True
) -> PlanTaskItem:
    _require_plan_manager(membership)
    _check_draft_version(item.version)
    unit, min_value, max_value = _clean_measurement(item_type, unit, min_value, max_value)
    required = required or item_type == ItemType.PHOTO
    before = audit.snapshot(item, ITEM_FIELDS)
    item.text, item.item_type, item.unit = text.strip(), item_type, unit
    item.min_value, item.max_value, item.required = min_value, max_value, required
    item.save(update_fields=[f for f in ITEM_FIELDS if f != "order"] + ["updated_at"])
    changes = audit.diff(before, audit.snapshot(item, ITEM_FIELDS))
    if changes:
        audit.record(action=AuditEvent.Action.UPDATE, target=item, membership=membership, changes=changes)
    return item


def _renumber(version) -> list[PlanTaskItem]:
    items = list(version.items.order_by("order", "created_at"))
    for number, item in enumerate(items, start=1):
        if item.order != number:
            item.order = number
            item.save(update_fields=["order", "updated_at"])
    return items


@transaction.atomic
def remove_draft_item(*, membership, item) -> None:
    _require_plan_manager(membership)
    version = item.version
    _check_draft_version(version)
    audit.record(
        action=AuditEvent.Action.DELETE,
        target=item,
        membership=membership,
        changes=audit.snapshot(item, ITEM_FIELDS),
    )
    item.delete()
    _renumber(version)


@transaction.atomic
def move_draft_item(*, membership, item, direction: str) -> None:
    """Sube o baja un ítem un lugar (intercambia el orden con el vecino)."""
    _require_plan_manager(membership)
    _check_draft_version(item.version)
    items = _renumber(item.version)
    index = next(i for i, other in enumerate(items) if other.pk == item.pk)
    target = index - 1 if direction == "up" else index + 1
    if direction not in ("up", "down") or not 0 <= target < len(items):
        return
    current, neighbour = items[index], items[target]
    current.order, neighbour.order = neighbour.order, current.order
    current.save(update_fields=["order", "updated_at"])
    neighbour.save(update_fields=["order", "updated_at"])


def _clean_resource(kind, description, quantity, part=None, unit="", keep=None):
    """Valida un recurso y devuelve (descripción, unidad, repuesto).

    Con repuesto del catálogo (INV-11 CA1): solo para tipo repuesto, de la
    organización del contexto y activo; la descripción vacía se llena con su
    nombre (es editable) y la unidad vacía con la del repuesto.
    """
    if kind not in PlanResource.Kind.values:
        raise ValidationError(_("Tipo no válido."))
    if quantity is None or quantity <= 0:
        raise ValidationError({"quantity": _("La cantidad debe ser mayor que cero.")})
    description, unit = (description or "").strip(), (unit or "").strip()
    if part is not None:
        part = _catalog_part(part, keep=keep)
        if kind != PlanResource.Kind.PART:
            raise ValidationError({"part": _("Solo un repuesto se enlaza al catálogo.")})
        description = description or part.name[:200]
        unit = unit or part.unit
    if not description:
        raise ValidationError({"description": _("Este campo es obligatorio.")})
    return description, unit, part


def _catalog_part(part, keep=None):
    """Repuesto activo de la organización del contexto (el manager filtra por organización).

    `keep`: el repuesto ya enlazado, que se conserva aunque se haya desactivado.
    """
    from apps.inventory.models import Part

    pk = getattr(part, "pk", part)
    found = Part.objects.filter(pk=pk).first()
    if found is None or not (found.is_active or str(pk) == str(keep)):
        raise ValidationError({"part": _("Repuesto no encontrado.")})
    return found


@transaction.atomic
def add_draft_resource(
    *, membership, version, kind, description, quantity, unit="", part=None
) -> PlanResource:
    _require_plan_manager(membership)
    _check_draft_version(version)
    description, unit, part = _clean_resource(kind, description, quantity, part, unit)
    resource = PlanResource.objects.create(
        version=version, kind=kind, description=description, quantity=quantity, unit=unit, part=part
    )
    audit.record(
        action=AuditEvent.Action.CREATE,
        target=resource,
        membership=membership,
        changes=audit.snapshot(resource, RESOURCE_FIELDS),
    )
    return resource


@transaction.atomic
def update_draft_resource(
    *, membership, resource, kind, description, quantity, unit="", part=None
) -> PlanResource:
    """Edita un recurso del borrador; `part=None` quita el enlace (en borrador se puede)."""
    _require_plan_manager(membership)
    _check_draft_version(resource.version)
    before = audit.snapshot(resource, RESOURCE_FIELDS)
    description, unit, part = _clean_resource(kind, description, quantity, part, unit, keep=resource.part_id)
    resource.kind, resource.description, resource.part = kind, description, part
    resource.quantity, resource.unit = quantity, unit
    resource.save(update_fields=[*RESOURCE_FIELDS, "updated_at"])
    changes = audit.diff(before, audit.snapshot(resource, RESOURCE_FIELDS))
    if changes:
        audit.record(action=AuditEvent.Action.UPDATE, target=resource, membership=membership, changes=changes)
    return resource


@transaction.atomic
def link_published_resource(*, membership, resource, part) -> PlanResource:
    """Enlaza al catálogo un repuesto de una versión publicada (INV-11 CA2).

    Excepción acotada al ADR-016: solo vacío → repuesto, una vez; nunca cambia
    ni quita un enlace, y la descripción, cantidad y unidad quedan iguales (lo
    que el plan pide no cambia). En un borrador se usa `update_draft_resource`.
    """
    _require_plan_manager(membership)
    part = _catalog_part(part)
    resource = PlanResource.objects.select_for_update().select_related("version").get(pk=resource.pk)
    return link_resources([resource], part=part, membership=membership)[0]


def link_resources(resources, *, part, membership) -> list[PlanResource]:
    """Enlaza recursos tipo repuesto sin `part` (borradores o publicados), auditando cada uno.

    Primitiva sin permisos: la llaman `link_published_resource` (plan.manage) y
    «Crear catálogo desde los planes» (inventory.manage).

    `DraftContentMixin.save()` rechaza cualquier cambio en una versión
    publicada; para este único campo, y solo si sigue vacío, se escribe con
    `QuerySet.update()` (el filtro `part__isnull=True` evita cambiar un enlace
    aunque otra transacción se adelante). Es la única escritura sobre contenido
    publicado (excepción documentada en el ADR-016).
    """
    for resource in resources:
        if resource.kind != PlanResource.Kind.PART:
            raise ValidationError({"part": _("Solo un repuesto se enlaza al catálogo.")})
        if resource.part_id is not None:
            raise ValidationError(_("Este recurso ya está enlazado al catálogo."))
    linked = PlanResource.objects.filter(
        pk__in=[r.pk for r in resources], kind=PlanResource.Kind.PART, part__isnull=True
    ).update(part=part, updated_at=timezone.now())
    if linked != len(resources):
        raise ValidationError(_("Este recurso ya está enlazado al catálogo."))
    for resource in resources:
        audit.record(
            action=AuditEvent.Action.UPDATE,
            target=resource,
            membership=membership,
            changes={"part": [None, str(part.pk)], "published": not resource.version.is_draft},
        )
    for resource in resources:
        resource.part = part
    return resources


@transaction.atomic
def remove_draft_resource(*, membership, resource) -> None:
    _require_plan_manager(membership)
    _check_draft_version(resource.version)
    audit.record(
        action=AuditEvent.Action.DELETE,
        target=resource,
        membership=membership,
        changes=audit.snapshot(resource, RESOURCE_FIELDS),
    )
    resource.delete()


@transaction.atomic
def publish_draft(*, membership, version: PlanVersion, change_note: str = "") -> PlanVersion:
    """Publica el borrador con `publish_version` (queda inmutable y recalcula vencimientos)."""
    _require_plan_manager(membership)
    return publish_version(version=version, by=membership.user, change_note=change_note.strip())


@transaction.atomic
def discard_draft(*, membership, version: PlanVersion) -> None:
    """Descarta el borrador con su contenido. Las versiones publicadas no se tocan."""
    _require_plan_manager(membership)
    version = PlanVersion.objects.select_for_update().get(pk=version.pk)
    _check_draft_version(version)
    audit.record(
        action=AuditEvent.Action.DELETE,
        target=version,
        membership=membership,
        changes={"number": version.number, "status": version.status},
    )
    version.resources.all().delete()
    version.items.all().delete()
    version.delete()


# --- Asignaciones desde la web -------------------------------------------------


def _visible_assets(membership):
    from apps.assets.selectors import visible_assets

    return visible_assets(membership=membership)


@transaction.atomic
def assign_plan_to_assets(*, membership, plan: MaintenancePlan, targets) -> tuple[int, list[str]]:
    """Asigna un plan a varios activos (PM-04 CA1: individual o en lote).

    `targets`: pares (activo, medidor o None). Solo activos dentro del alcance
    de la membresía. Idempotente: un activo que ya tiene el plan activo se
    salta (no se reinicia su vencimiento). Devuelve (asignados, avisos).
    """
    _require_plan_manager(membership)
    if not plan.is_active:
        raise ValidationError(_("El plan está inactivo."))
    if plan.current_version is None:
        raise PlanVersionError(_("«%(plan)s» no tiene una versión publicada.") % {"plan": plan})
    targets = list(targets)
    ids = [asset.pk for asset, _meter in targets]
    visible = set(_visible_assets(membership).filter(pk__in=ids).values_list("pk", flat=True))
    already = set(
        plan.assignments.filter(is_active=True, asset_id__in=visible).values_list("asset_id", flat=True)
    )
    assigned, warnings = 0, []
    for asset, meter in targets:
        if asset.pk not in visible:
            raise ValidationError(_("%(asset)s no está en tu alcance.") % {"asset": asset.code})
        if asset.pk in already:
            continue
        if meter is not None and (meter.asset_id != asset.pk or meter.kind != plan.meter_kind):
            raise ValidationError(_("El medidor elegido no corresponde a %(asset)s.") % {"asset": asset.code})
        try:
            with transaction.atomic():
                assign_plan(plan=plan, asset=asset, meter=meter, by=membership.user)
        except ValidationError as error:
            warnings.extend(f"{asset.code}: {message}" for message in error.messages)
        else:
            assigned += 1
            already.add(asset.pk)
    return assigned, warnings


@transaction.atomic
def assign_plan_to_class(*, membership, plan: MaintenancePlan) -> tuple[int, list[str]]:
    """«Asignar a todos los activos de la clase» (PM-04 CA1), dentro del alcance por sitio."""
    _require_plan_manager(membership)
    if plan.asset_class_id is None:
        raise ValidationError(_("El plan no tiene una clase de activo."))
    assets = (
        _visible_assets(membership)
        .filter(asset_class_id=plan.asset_class_id, is_active=True)
        .exclude(status="retired")
        .exclude(pk__in=plan.assignments.filter(is_active=True).values("asset_id"))
    )
    return assign_plan_to_assets(membership=membership, plan=plan, targets=[(a, None) for a in assets])


def assign_class_plans(*, assets, by=None) -> tuple[int, list[str]]:
    """Aplica a activos nuevos los planes activos de su clase (PM-04 CA1, automática por clase).

    La usan la importación Excel y el alta de un activo. Un plan por uso sin el
    medidor que necesita no bloquea: queda como aviso.
    """
    assets = [a for a in assets if a.asset_class_id]
    if not assets:
        return 0, []
    plans_by_class = {}
    for plan in MaintenancePlan.objects.filter(
        is_active=True,
        current_version__isnull=False,
        asset_class_id__in={a.asset_class_id for a in assets},
    ):
        plans_by_class.setdefault(plan.asset_class_id, []).append(plan)
    assigned, warnings = 0, []
    for asset in assets:
        for plan in plans_by_class.get(asset.asset_class_id, []):
            try:
                with transaction.atomic():
                    assign_plan(plan=plan, asset=asset, by=by)
            except ValidationError:
                warnings.append(
                    _("%(code)s: no se asignó «%(plan)s» (falta el medidor que usa el plan).")
                    % {"code": asset.code, "plan": plan.name}
                )
            else:
                assigned += 1
    return assigned, warnings


@transaction.atomic
def deactivate_assignment(*, membership, assignment: PlanAssignment) -> PlanAssignment:
    """Quita el plan de un activo sin borrar su historial. Idempotente."""
    _require_plan_manager(membership)
    if not _visible_assets(membership).filter(pk=assignment.asset_id).exists():
        raise ValidationError(_("El activo no está en tu alcance."))
    assignment = PlanAssignment.objects.select_for_update().get(pk=assignment.pk)
    if assignment.is_active:
        assignment.is_active = False
        assignment.save(update_fields=["is_active", "updated_at"])
        audit.record(
            action=AuditEvent.Action.UPDATE,
            target=assignment,
            membership=membership,
            changes={"is_active": [True, False]},
        )
    return assignment


# --- Ajustes del programador ---------------------------------------------------


@transaction.atomic
def update_maintenance_settings(*, membership, horizon_days: int) -> MaintenanceSettings:
    """Horizonte de generación de la organización (PM-05 CA1). Solo administradores."""
    require_perm(membership, Perm.ORGANIZATION_MANAGE)
    if not 1 <= horizon_days <= MAX_HORIZON_DAYS:
        raise ValidationError({"horizon_days": _("Usa entre 1 y %(max)s días.") % {"max": MAX_HORIZON_DAYS}})
    settings_row, _created = MaintenanceSettings.objects.select_for_update().get_or_create()
    before = settings_row.horizon_days if not _created else DEFAULT_HORIZON_DAYS
    if before != horizon_days or _created:
        settings_row.horizon_days = horizon_days
        settings_row.save(update_fields=["horizon_days", "updated_at"])
        audit.record(
            action=AuditEvent.Action.UPDATE,
            target=settings_row,
            membership=membership,
            changes={"horizon_days": [before, horizon_days]},
        )
    return settings_row


# --- Absorción de planes (PM-05 CA3) ------------------------------------------------


def _absorbs_transitively(start_id, target_id) -> bool:
    """¿`start` ya absorbe (directa o indirectamente) a `target`? Evita ciclos A ⊃ B ⊃ A."""
    edges = {}
    for plan_id, absorbed_id in PlanAbsorption.objects.values_list("plan_id", "absorbed_plan_id"):
        edges.setdefault(plan_id, set()).add(absorbed_id)
    pending, seen = [start_id], set()
    while pending:
        current = pending.pop()
        if current == target_id:
            return True
        if current not in seen:
            seen.add(current)
            pending.extend(edges.get(current, ()))
    return False


@transaction.atomic
def add_plan_absorption(
    *, membership, plan: MaintenancePlan, absorbed_plan: MaintenancePlan
) -> PlanAbsorption:
    """Configura que `plan` (mayor) absorba a `absorbed_plan` (menor) en el mismo activo. Idempotente."""
    _require_plan_manager(membership)
    if (
        absorbed_plan.organization_id != plan.organization_id
        or plan.organization_id != membership.organization_id
    ):
        raise ValidationError(_("Elige un plan de esta organización."))
    if absorbed_plan.pk == plan.pk:
        raise ValidationError(_("Un plan no puede absorberse a sí mismo."))
    locked_plans().get(pk=plan.pk)
    existing = PlanAbsorption.objects.filter(plan=plan, absorbed_plan=absorbed_plan).first()
    if existing is not None:
        return existing
    if _absorbs_transitively(absorbed_plan.pk, plan.pk):
        raise ValidationError(
            _("«%(other)s» ya absorbe a «%(plan)s»: no pueden absorberse mutuamente.")
            % {"other": absorbed_plan.name, "plan": plan.name}
        )
    absorption = PlanAbsorption.objects.create(plan=plan, absorbed_plan=absorbed_plan)
    audit.record(
        action=AuditEvent.Action.CREATE,
        target=plan,
        membership=membership,
        changes={"absorbs": [None, absorbed_plan.code]},
    )
    return absorption


@transaction.atomic
def remove_plan_absorption(*, membership, absorption: PlanAbsorption) -> None:
    """Deja de absorber. Las OT ya generadas con ambos checklists no cambian."""
    _require_plan_manager(membership)
    audit.record(
        action=AuditEvent.Action.UPDATE,
        target=absorption.plan,
        membership=membership,
        changes={"absorbs": [absorption.absorbed_plan.code, None]},
    )
    absorption.delete()
