"""Proyección de preventivos futuros (PM-06 CA1), en memoria y sin crear OT.

Simula el programador activo por activo: cada ocurrencia vence por fecha o por
uso (lo que ocurra primero, con el uso diario promedio del medidor), se supone
ejecutada en su fecha y se reprograma con la misma regla que al cerrar la OT
(`scheduling.next_schedule`, flotante o fijo). Los planes que absorben a otros
se revisan primero en cada fecha y aplican `scheduling.absorbs`, igual que el
generador de OT.
"""

import datetime
from dataclasses import dataclass, field
from decimal import Decimal

from .scheduling import absorbs, estimated_meter_date, next_schedule

# Tope de ocurrencias por proyección: sobre esto se pide filtrar.
MAX_OCCURRENCES = 3000


@dataclass
class Occurrence:
    date: datetime.date
    plan: object
    asset: object
    version: object = None
    assignment: object = None
    work_order: object = None
    # "calendar" o "meter": qué disparó la ocurrencia.
    trigger: str = "calendar"
    overdue: bool = False
    # El medidor no tiene uso promedio: solo se proyecta por calendario.
    no_usage: bool = False
    absorbed_into: "Occurrence | None" = None
    absorbed: list = field(default_factory=list)

    @property
    def is_work_order(self):
        return self.work_order is not None

    @property
    def estimated_minutes(self):
        """Minutos de la OT que resultaría (con los planes absorbidos); 0 si está absorbida."""
        if self.work_order is not None:
            return self.work_order.estimated_minutes or 0
        if self.absorbed_into is not None:
            return 0
        return self.version.estimated_minutes + sum(o.version.estimated_minutes for o in self.absorbed)


@dataclass
class _State:
    assignment: object
    version: object
    due_on: datetime.date | None
    meter_target: Decimal | None
    skip_first: bool
    done: bool = False
    emitted: bool = False


def _reading(meter, date, today):
    """Lectura estimada del medidor en `date` (actual + uso promedio · días)."""
    if meter is None or meter.current_value is None:
        return None
    avg = meter.avg_daily_usage or Decimal(0)
    return meter.current_value + avg * max((date - today).days, 0)


def _next(state, today):
    """(fecha cruda, disparador, sin uso) del próximo vencimiento; fecha None si no se puede estimar."""
    candidates = []
    no_usage = False
    if state.due_on is not None and state.version.calendar_interval:
        candidates.append((state.due_on, "calendar"))
    meter = state.assignment.meter
    if state.version.uses_meter and meter is not None and state.meter_target is not None:
        if meter.current_value is None:
            no_usage = True
        else:
            date = estimated_meter_date(
                current=meter.current_value,
                target=state.meter_target,
                avg_daily_usage=meter.avg_daily_usage,
                today=today,
            )
            if date is None:
                no_usage = True
            else:
                candidates.append((date, "meter"))
    if not candidates:
        return None, "", no_usage
    date, trigger = min(candidates)
    return date, trigger, no_usage


def _complete(state, date, today):
    meter = state.assignment.meter if state.assignment.meter_id else None
    state.due_on, state.meter_target = next_schedule(
        version=state.version,
        next_due_on=state.due_on,
        next_due_meter_value=state.meter_target,
        completed_on=date,
        meter_value=_reading(meter, date, today) if state.version.uses_meter else None,
        anchor_day=state.assignment.anchor_day,
    )


def project(*, assignments, absorptions, busy, today, end):
    """Ocurrencias entre hoy y `end` (inclusive) de las asignaciones dadas.

    `assignments`: asignaciones activas con plan__current_version, asset y meter
    ya cargados. `absorptions`: {plan_id: {absorbed_plan_id, …}}. `busy`: ids de
    asignaciones con una OT abierta: esa OT es su próxima ocurrencia y la
    proyección sigue desde la siguiente. Devuelve (ocurrencias, sin_estimar,
    truncada): `sin_estimar` son las asignaciones por medidor sin uso promedio
    que no tienen ninguna fecha proyectable.
    """
    by_asset = {}
    for assignment in assignments:
        by_asset.setdefault(assignment.asset_id, []).append(
            _State(
                assignment=assignment,
                version=assignment.plan.current_version,
                due_on=assignment.next_due_on,
                meter_target=assignment.next_due_meter_value,
                skip_first=assignment.id in busy,
            )
        )
    occurrences, unestimated, count = [], [], 0
    for states in by_asset.values():
        while True:
            pending = []
            for state in states:
                if state.done:
                    continue
                raw, trigger, no_usage = _next(state, today)
                if raw is None:
                    state.done = True
                    if no_usage and not state.emitted and not state.skip_first:
                        unestimated.append(state.assignment)
                    continue
                date = max(raw, today)
                if date > end:
                    state.done = True
                    continue
                absorber = state.assignment.plan_id in absorptions
                name = state.assignment.plan.name
                pending.append((date, not absorber, name, raw, trigger, no_usage, state))
            if not pending:
                break
            date, _minor, _name, raw, trigger, no_usage, state = min(pending, key=lambda row: row[:3])
            if state.skip_first:  # su OT abierta ya es esta ocurrencia (se lista aparte)
                state.skip_first, state.emitted = False, True
                _complete(state, date, today)
                continue
            occurrence = Occurrence(
                date=date,
                plan=state.assignment.plan,
                asset=state.assignment.asset,
                version=state.version,
                assignment=state.assignment,
                trigger=trigger,
                overdue=raw < today,
                no_usage=no_usage,
            )
            for other in states:
                if (
                    other is state
                    or other.done
                    or other.skip_first
                    or other.assignment.plan_id not in absorptions.get(state.assignment.plan_id, ())
                    or not absorbs(
                        version=state.version,
                        minor_version=other.version,
                        due_on=raw,
                        meter_target=state.meter_target if state.assignment.meter_id else None,
                        meter_id=state.assignment.meter_id,
                        other_due_on=other.due_on,
                        other_meter_target=other.meter_target if other.assignment.meter_id else None,
                        other_meter_id=other.assignment.meter_id,
                    )
                ):
                    continue
                minor = Occurrence(
                    date=date,
                    plan=other.assignment.plan,
                    asset=other.assignment.asset,
                    version=other.version,
                    assignment=other.assignment,
                    absorbed_into=occurrence,
                )
                occurrence.absorbed.append(minor)
                occurrences.append(minor)
                other.emitted = True
                _complete(other, date, today)
            occurrences.append(occurrence)
            state.emitted = True
            _complete(state, date, today)
            count += 1 + len(occurrence.absorbed)
            if count >= MAX_OCCURRENCES:
                occurrences.sort(key=_sort_key)
                return occurrences, unestimated, True
    occurrences.sort(key=_sort_key)
    return occurrences, unestimated, False


def _sort_key(occurrence):
    absorbed = occurrence.absorbed_into is not None
    return (occurrence.date, occurrence.asset.code, absorbed, occurrence.plan.name)
