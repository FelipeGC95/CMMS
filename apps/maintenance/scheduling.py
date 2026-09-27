"""Reglas puras del programador de preventivos (PM-02, PM-05), sin base de datos.

Las usan el generador de OT (`services`) y la proyección del calendario
(`selectors.plan_calendar`, PM-06): así ambos calculan vencimientos, fechas
estimadas por uso y absorciones con la misma regla.
"""

import datetime
import math
from decimal import Decimal

from .models import CalendarUnit, PlanVersion, with_anchor_day


def estimated_meter_date(*, current, target, avg_daily_usage, today: datetime.date) -> datetime.date | None:
    """Fecha en que el medidor llegará a `target` según su uso diario promedio.

    Si ya lo alcanzó, hoy. Sin uso promedio conocido no hay fecha (None).
    """
    remaining = target - current
    if remaining <= 0:
        return today
    if not avg_daily_usage or avg_daily_usage <= 0:
        return None
    return today + datetime.timedelta(days=math.ceil(remaining / avg_daily_usage))


def absorbs(
    *,
    version: PlanVersion,
    minor_version: PlanVersion,
    due_on: datetime.date | None,
    meter_target,
    meter_id,
    other_due_on: datetime.date | None,
    other_meter_target,
    other_meter_id,
) -> bool:
    """¿El plan menor vence junto al mayor en el mismo activo? (PM-05 CA3)

    «Junto» = a más tardar en el vencimiento del mayor más su tolerancia
    (`tolerance_pct` del plan mayor aplicado a su frecuencia): por fecha, hasta
    `due_on` + tolerancia en días; por medidor (el mismo medidor), hasta la
    lectura objetivo del mayor + tolerancia en unidades. Un menor ya vencido
    también entra: la OT del mayor lo cubre.
    """
    pct = Decimal(version.tolerance_pct) / 100
    if other_due_on is not None and due_on is not None:
        # Tolerancia en días sobre la frecuencia por calendario del mayor (o, si no tiene, la del menor).
        reference = version if version.calendar_interval else minor_version
        period = reference.calendar_period_days(due_on) if reference.calendar_interval else 0
        if other_due_on <= due_on + datetime.timedelta(days=math.floor(period * pct)):
            return True
    if (
        other_meter_target is not None
        and meter_target is not None
        and other_meter_id is not None
        and other_meter_id == meter_id
        and version.uses_meter
    ):
        return other_meter_target <= meter_target + version.meter_interval * pct
    return False


def next_fixed_target(previous, interval, after):
    """Modo fijo: siguiente objetivo = anterior + k·intervalo, el primero posterior a `after`.

    Así, una OT cerrada con más de un intervalo de atraso no genera OT en el pasado.
    """
    target = previous + interval
    if target <= after:
        periods = math.floor((after - previous) / interval) + 1
        target = previous + interval * periods
    return target


def next_fixed_date(
    previous: datetime.date, version: PlanVersion, after: datetime.date, anchor_day: int | None = None
) -> datetime.date:
    """Modo fijo por calendario: anterior + k·frecuencia, el primero posterior a `after`.

    En meses, cada vencimiento vuelve al día ancla de la asignación (el del
    primer vencimiento): 31-ene → 28-feb → 31-mar, aunque la fecha anterior se
    haya ajustado a fin de mes. Sin ancla se usa el día de `previous`.
    """
    if version.calendar_unit != CalendarUnit.MONTHS:
        days = version.calendar_interval * (7 if version.calendar_unit == CalendarUnit.WEEKS else 1)
        return next_fixed_target(previous, datetime.timedelta(days=days), after)
    anchor_day = anchor_day or previous.day
    months = (after.year - previous.year) * 12 + after.month - previous.month
    k = max(1, months // version.calendar_interval - 1)
    while with_anchor_day(version.shift(previous, k), anchor_day) <= after:
        k += 1
    return with_anchor_day(version.shift(previous, k), anchor_day)


def next_schedule(
    *, version: PlanVersion, next_due_on, next_due_meter_value, completed_on, meter_value, anchor_day=None
):
    """Próximo vencimiento (fecha, lectura) tras ejecutar el plan el `completed_on` con `meter_value`.

    Flotante: desde la ejecución real (fecha y lectura). Fijo: desde lo
    programado, sin quedar en el pasado. `meter_value` None deja la lectura
    objetivo como estaba (no hay con qué recalcularla).
    """
    fixed = version.schedule_mode == PlanVersion.ScheduleMode.FIXED
    if version.calendar_interval:
        if fixed and next_due_on is not None:
            next_due_on = next_fixed_date(next_due_on, version, completed_on, anchor_day)
        else:
            next_due_on = version.shift(completed_on)
    else:
        next_due_on = None
    if version.uses_meter and meter_value is not None:
        if fixed and next_due_meter_value is not None:
            interval = version.meter_interval
            next_due_meter_value = next_fixed_target(next_due_meter_value, interval, meter_value)
        else:
            next_due_meter_value = meter_value + version.meter_interval
    return next_due_on, next_due_meter_value
