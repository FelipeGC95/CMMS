"""Mantenimiento por condición de la demo (CBM-10 CA1): variables, reglas e historia.

Lo usa `seed_demo`. Variables en varios activos (temperatura, vibración, presión,
corriente), reglas por clase de activo y por activo (`gt`, `lt`, `outside`,
`rate`) y 90 días de lecturas con ruido plausible para la tendencia de la ficha.

La historia antigua (en rango, antes de que existan las reglas) va por
`bulk_create`: no se evalúa, así que no deja estados ni alertas. Los últimos
`LIVE_DAYS` días pasan por el servicio real (`record_meter_reading` →
`evaluate_reading`), día por día y en orden cronológico: así los estados
(`ConditionState`) quedan coherentes con la última lectura y los episodios de
alarma (alertas y OT predictivas) los crea la evaluación, no este archivo. Solo
se mueven al pasado las fechas de creación de las alertas y de la OT predictiva
cerrada (solo datos demo).
"""

import datetime
import math
import random
from decimal import Decimal

from apps.assets.models import MeterKind, MeterReading
from apps.assets.services import create_variable, record_meter_reading, set_variable_active
from apps.condition import services as condition
from apps.condition.models import ConditionAlert, ConditionRule
from apps.work.models import WorkOrder
from apps.work.services import complete_work_order

TEMP, VIB, PRES, CURR = MeterKind.TEMPERATURE, MeterKind.VIBRATION, MeterKind.PRESSURE, MeterKind.CURRENT
GT, LT = ConditionRule.Operator.GT, ConditionRule.Operator.LT
OUTSIDE, RATE = ConditionRule.Operator.OUTSIDE, ConditionRule.Operator.RATE
ALERT, ALERT_WO = ConditionRule.Action.ALERT, ConditionRule.Action.ALERT_WORK_ORDER

HISTORY_DAYS = 90  # lo que muestra la tendencia de la ficha (CBM-07)
LIVE_DAYS = 10  # últimos días: lecturas por el servicio, con evaluación de reglas

# activo, tipo, nombre, valor típico, ruido (desviación), amplitud de la onda mensual.
# Valor típico None: variable sin lecturas ("Sin lecturas" en la ficha).
VARIABLES = [
    ("CMP-01", TEMP, "Descarga", 84, 1.2, 2.0),
    ("CMP-02", TEMP, "Descarga", 79, 1.2, 2.0),
    ("CMP-01", VIB, "Rodamiento lado acople", 2.6, 0.2, 0.3),
    ("CMP-02", VIB, "Rodamiento lado acople", None, 0, 0),
    ("CMP-01", PRES, "Línea de aire", 7.3, 0.1, 0.1),
    ("ENV-01", TEMP, "Mordaza vertical", 156, 2.0, 2.0),
    ("ENV-02", TEMP, "Mordaza vertical", 154, 2.0, 2.0),
    ("ENV-01", CURR, "Motor de arrastre", 11.2, 0.35, 0.4),
    ("FRI-01", TEMP, "Cámara", -19.5, 0.5, 0.6),
    ("FRI-02", TEMP, "Cámara", -20.0, 0.5, 0.6),
    ("FRI-02", CURR, "Compresor", 21.0, 0.6, 0.8),
    ("GEN-01", TEMP, "Refrigerante", 78, 1.2, 1.5),
    ("CAL-01", PRES, "Vapor", 7.6, 0.15, 0.2),
    ("GRH-02", TEMP, "Aceite hidráulico", 48, 2.0, 3.0),  # sin reglas
    ("GRH-01", TEMP, "Motor de tracción", 55, 2.0, 2.0),  # se desactiva (CBM-01 CA5)
]
DEACTIVATED = {("GRH-01", TEMP, "Motor de tracción")}

# Reglas por clase: clase, tipo, nombre de variable, operador, valores, seguidas, acción, extra.
CLASS_RULES = [
    ("CMP", TEMP, "Descarga", GT, {"threshold": "95"}, 2, ALERT_WO, {
        "name": "Temperatura de descarga alta",
        "work_order_title": "Revisar enfriador y aceite del compresor",
        "work_order_priority": ConditionRule.Priority.HIGH,
        "work_order_instructions": (
            "Limpiar el enfriador, revisar el nivel de aceite y la válvula termostática. "
            "Medir la temperatura de descarga al terminar."
        ),
    }),
    ("CMP", VIB, "Rodamiento lado acople", GT, {"threshold": "7.1"}, 1, ALERT, {
        "name": "Vibración alta en rodamiento (zona C, ISO 10816)",
    }),
    ("ENV", TEMP, "Mordaza vertical", OUTSIDE, {"min_value": "140", "max_value": "170"}, 1, ALERT, {
        "name": "Temperatura de sellado fuera de rango",
    }),
    ("FRI", TEMP, "Cámara", GT, {"threshold": "-15"}, 1, ALERT, {
        "name": "Cámara sobre -15 °C",
    }),
]  # fmt: skip

# Reglas por activo: activo, tipo, nombre, operador, valores, seguidas, acción, extra.
ASSET_RULES = [
    ("FRI-02", CURR, "Compresor", GT, {"threshold": "26"}, 2, ALERT_WO, {
        "work_order_title": "Revisar consumo del compresor de la cámara 2",
        "work_order_instructions": "Medir corriente por fase, revisar contactor y presión de alta.",
    }),
    ("CMP-01", PRES, "Línea de aire", OUTSIDE, {"min_value": "6.5", "max_value": "8"}, 3, ALERT, {}),
    ("GEN-01", TEMP, "Refrigerante", RATE, {"threshold": "12"}, 1, ALERT, {
        "name": "Subida brusca de temperatura del refrigerante",
    }),
    ("CAL-01", PRES, "Vapor", LT, {"threshold": "6"}, 2, ALERT_WO, {
        "work_order_title": "Revisar quemador y presostato de la caldera",
    }),
    ("ENV-01", CURR, "Motor de arrastre", GT, {"threshold": "14"}, 2, ALERT, {}),
]  # fmt: skip
# Regla reemplazada (queda inactiva, sin borrado físico): umbral anterior del motor de arrastre.
REPLACED_RULE = ("ENV-01", CURR, "Motor de arrastre", GT, {"threshold": "13"}, 2, ALERT, {})

# Episodios en los últimos días (días atrás → valor), evaluados por el servicio:
EPISODES = {
    # Abierta con OT predictiva: dos lecturas seguidas sobre 95 °C (la última, hoy).
    ("CMP-01", TEMP, "Descarga"): {2: 93.4, 1: 96.2, 0: 98.1},
    # Reconocida (solo alerta): la mordaza sobre 170 °C desde ayer.
    ("ENV-01", TEMP, "Mordaza vertical"): {1: 173.5, 0: 174.2},
    # Normalizada: vibración alta, se lubricó y volvió a rango.
    ("CMP-01", VIB, "Rodamiento lado acople"): {6: 7.6, 5: 7.9, 4: 3.1},
    # Cerrada: OT predictiva completada (`CLOSE_ON_DAY`) y la corriente volvió a rango.
    ("FRI-02", CURR, "Compresor"): {9: 27.4, 8: 28.1},
    # Abierta (solo alerta): la cámara 1 se calienta, igual que el aviso de falla de hoy.
    ("FRI-01", TEMP, "Cámara"): {1: -16.8, 0: -8.0},
}
CLOSE_ON_DAY = 7  # la OT predictiva de la corriente de la cámara 2 se completa ese día


def _value(rng, base, noise, wave, days_ago) -> Decimal:
    value = base + wave * math.sin(2 * math.pi * days_ago / 30) + rng.gauss(0, noise)
    return Decimal(str(round(value, 2 if abs(base) < 10 else 1)))


def _read_at(now, days_ago, rng) -> datetime.datetime:
    # Ronda de la mañana, unas horas antes de "ahora": nunca en el futuro, ~1 día entre lecturas.
    return now - datetime.timedelta(days=days_ago, hours=3) + datetime.timedelta(minutes=rng.randint(0, 40))


def seed(*, org, assets, classes, admin, tech, now):
    """Variables, reglas, historia de 90 días y los episodios de la demo."""
    rng = random.Random(29)  # propio: no cambia la secuencia del resto de seed_demo
    meters = {}
    for code, kind, name, base, noise, wave in VARIABLES:
        meter = create_variable(membership=admin, asset=assets[code], kind=kind, name=name)
        meters[(code, kind, name)] = (meter, base, noise, wave)

    # Historia antigua en rango (no se evalúa: aún no hay reglas).
    rows = []
    for (_code, _kind, _name), (meter, base, noise, wave) in meters.items():
        if base is None:
            continue
        for days_ago in range(HISTORY_DAYS, LIVE_DAYS, -1):
            rows.append(
                MeterReading(
                    organization=org,
                    meter=meter,
                    value=_value(rng, base, noise, wave, days_ago),
                    read_at=_read_at(now, days_ago, rng),
                    created_by=tech.user,
                )
            )
    MeterReading.objects.bulk_create(rows)
    for meter, base, _noise, _wave in meters.values():
        last = meter.readings.order_by("-read_at").first() if base is not None else None
        if last is not None:
            meter.current_value, meter.current_read_at = last.value, last.read_at
            meter.save(update_fields=["current_value", "current_read_at", "updated_at"])

    for key in DEACTIVATED:
        set_variable_active(membership=admin, meter=meters[key][0], is_active=False)

    for cls, kind, name, operator, values, consecutive, action, extra in CLASS_RULES:
        condition.create_rule(
            membership=admin,
            asset_class=classes[cls],
            meter_kind=kind,
            meter_name=name,
            operator=operator,
            consecutive=consecutive,
            action=action,
            **values,
            **extra,
        )
    replaced = _asset_rule(admin, meters, REPLACED_RULE)
    condition.set_rule_active(membership=admin, rule=replaced, is_active=False)
    for spec in ASSET_RULES:
        _asset_rule(admin, meters, spec)

    # Últimos días por el servicio, en orden cronológico entre todas las variables.
    for days_ago in range(LIVE_DAYS, -1, -1):
        if days_ago == CLOSE_ON_DAY:
            _complete_predictive(tech, meters[("FRI-02", CURR, "Compresor")][0], now, days_ago)
        for key, (meter, base, noise, wave) in meters.items():
            if base is None or key in DEACTIVATED:
                continue
            value = EPISODES.get(key, {}).get(days_ago)
            value = Decimal(str(value)) if value is not None else _value(rng, base, noise, wave, days_ago)
            record_meter_reading(
                meter=meter, value=value, read_at=_read_at(now, days_ago, rng), membership=tech
            )

    # Reconocida: la jefa vio la alerta de la mordaza y se hace cargo.
    mordaza = meters[("ENV-01", TEMP, "Mordaza vertical")][0]
    condition.acknowledge_alert(
        membership=admin, alert=ConditionAlert.objects.get(meter=mordaza, status=ConditionAlert.Status.OPEN)
    )
    # Solo demo: la alerta aparece creada cuando llegó la lectura que la abrió.
    for alert in ConditionAlert.objects.all():
        ConditionAlert.objects.filter(pk=alert.pk).update(created_at=alert.opened_at)
        if alert.work_order_id and alert.status == ConditionAlert.Status.OPEN:
            WorkOrder.objects.filter(pk=alert.work_order_id).update(created_at=alert.opened_at)
            alert.work_order.transitions.update(created_at=alert.opened_at)


def _asset_rule(admin, meters, spec):
    code, kind, name, operator, values, consecutive, action, extra = spec
    return condition.create_rule(
        membership=admin,
        meter=meters[(code, kind, name)][0],
        operator=operator,
        consecutive=consecutive,
        action=action,
        **values,
        **extra,
    )


def _complete_predictive(tech, meter, now, days_ago):
    """El técnico completa la OT predictiva: su alerta pasa a `closed` (CBM-05)."""
    alert = ConditionAlert.objects.select_related("work_order").get(meter=meter)
    work_order = alert.work_order
    complete_work_order(
        work_order=work_order,
        membership=tech,
        labor_minutes=90,
        notes="Contactor con contactos picados: se cambió y se reapretaron bornes. Consumo normal.",
    )
    done = now - datetime.timedelta(days=days_ago, hours=6)
    WorkOrder.objects.filter(pk=work_order.pk).update(
        created_at=alert.opened_at,
        started_at=done - datetime.timedelta(hours=2),
        completed_at=done,
        closed_at=done,
    )
    work_order.transitions.update(created_at=done)
    ConditionAlert.objects.filter(pk=alert.pk).update(closed_at=done)
