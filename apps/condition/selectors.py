"""Lecturas de mantenimiento por condición (CBM-05, CBM-07): permisos, alcance por sitio y sin N+1."""

import datetime
from dataclasses import dataclass, field
from decimal import Decimal

from django.db.models import Case, Count, IntegerField, Q, Value, When
from django.utils import timezone

from apps.accounts.permissions import Perm, require_perm
from apps.accounts.scoping import allowed_location_ids
from apps.assets.models import VARIABLE_KINDS, Asset, Meter, MeterReading
from apps.assets.selectors import visible_assets
from apps.organizations.selectors import descendant_ids

from .charts import TrendPoint
from .models import ConditionAlert, ConditionRule, ConditionState

Status = ConditionAlert.Status

# Abiertas primero, luego reconocidas, normalizadas y cerradas (CBM-05).
STATUS_ORDER = Case(
    When(status=Status.OPEN, then=Value(0)),
    When(status=Status.ACKNOWLEDGED, then=Value(1)),
    When(status=Status.NORMALIZED, then=Value(2)),
    default=Value(3),
    output_field=IntegerField(),
)
ALERT_RELATED = (
    "rule__asset_class",
    "rule__meter__asset",
    "meter__asset",
    "asset__location",
    "work_order",
    "acknowledged_by",
)


# --- Alertas ---------------------------------------------------------------------


def visible_alerts(*, membership):
    """Alertas de los activos que la membresía ve (ORG-06)."""
    require_perm(membership, Perm.CONDITION_VIEW)
    qs = ConditionAlert.objects.all()
    allowed = allowed_location_ids(membership)
    if allowed is not None:
        qs = qs.filter(asset__location_id__in=allowed)
    return qs


def alert_list(*, membership, status: str = "", location=None, asset=None):
    """Alertas abiertas primero y luego las más recientes; filtros por estado, sitio y activo.

    `status` = "active" filtra abiertas y reconocidas; `location` incluye sus sububicaciones.
    """
    qs = visible_alerts(membership=membership)
    if status == "active":
        qs = qs.filter(status__in=ConditionAlert.ACTIVE_STATUSES)
    elif status in Status.values:
        qs = qs.filter(status=status)
    if location is not None:
        qs = qs.filter(asset__location_id__in={location.pk, *descendant_ids(location)})
    if asset is not None:
        qs = qs.filter(asset=asset)
    return (
        qs.select_related(*ALERT_RELATED)
        .annotate(status_order=STATUS_ORDER)
        .order_by("status_order", "-created_at")
    )


def alert_for(*, membership, pk):
    """Una alerta visible para la membresía, o None."""
    return visible_alerts(membership=membership).select_related(*ALERT_RELATED).filter(pk=pk).first()


def active_alert_count(*, membership) -> int:
    """Alertas abiertas o reconocidas que la membresía ve (contador del inicio)."""
    return visible_alerts(membership=membership).filter(status__in=ConditionAlert.ACTIVE_STATUSES).count()


def alert_assets(*, membership, include=None):
    """Activos visibles con alguna alerta (opciones del filtro por activo), más `include` si se pide."""
    require_perm(membership, Perm.CONDITION_VIEW)
    assets = visible_assets(membership=membership)
    with_alerts = Q(pk__in=ConditionAlert.objects.values("asset_id"))
    if include is not None:
        with_alerts |= Q(pk=include)
    return assets.filter(with_alerts).order_by("code")


def asset_alerts(*, membership, asset, limit: int = 20) -> list:
    """Últimas alertas de un activo (ficha del activo, CBM-07)."""
    return list(alert_list(membership=membership, asset=asset)[:limit])


# --- Reglas ----------------------------------------------------------------------


def _visible_rule_scope(membership) -> Q:
    """Reglas por clase: todas. Reglas por medidor: solo si el activo está en el alcance."""
    allowed = allowed_location_ids(membership)
    if allowed is None:
        return Q()
    return Q(meter__isnull=True) | Q(meter__asset__location_id__in=allowed)


def visible_rules(*, membership, include_inactive: bool = False):
    require_perm(membership, Perm.CONDITION_VIEW)
    qs = ConditionRule.objects.filter(_visible_rule_scope(membership))
    if not include_inactive:
        qs = qs.filter(is_active=True)
    return qs


def rule_list(
    *, membership, include_inactive: bool = False, inactive_only: bool = False, asset_class=None, meter=None
):
    """Reglas (activas, todas o solo las inactivas) con su alcance; filtra por clase o por medidor."""
    qs = visible_rules(membership=membership, include_inactive=include_inactive or inactive_only)
    if inactive_only:
        qs = qs.filter(is_active=False)
    if asset_class is not None:
        qs = qs.filter(asset_class=asset_class)
    if meter is not None:
        qs = qs.filter(pk__in=rules_for_meter(meter, include_inactive=True).values("pk"))
    return qs.select_related("meter__asset", "asset_class").order_by("-is_active", "created_at")


def rule_for(*, membership, pk):
    return (
        visible_rules(membership=membership, include_inactive=True)
        .select_related("meter__asset", "asset_class")
        .filter(pk=pk)
        .first()
    )


def rules_for_meter(meter: Meter, *, include_inactive: bool = False):
    """Reglas que aplican a una variable: las del medidor y las de la clase de su activo con su
    (tipo, nombre). Una sola consulta (la clase sale por subconsulta)."""
    asset_class = Asset.objects.filter(pk=meter.asset_id).values("asset_class_id")
    qs = ConditionRule.objects.filter(
        Q(meter_id=meter.pk)
        | Q(meter__isnull=True, asset_class_id__in=asset_class, meter_kind=meter.kind, meter_name=meter.name)
    )
    if not include_inactive:
        qs = qs.filter(is_active=True)
    return qs.order_by("created_at")


def variable_meters(*, membership):
    """Variables activas de los activos activos visibles (alcance de una regla nueva, CBM-03)."""
    require_perm(membership, Perm.CONDITION_VIEW)
    return (
        Meter.objects.filter(
            kind__in=VARIABLE_KINDS,
            is_active=True,
            asset__is_active=True,
            asset__in=visible_assets(membership=membership),
        )
        .select_related("asset")
        .order_by("asset__code", "kind", "name")
    )


# --- Estado por variable (ficha del activo, CBM-07) --------------------------------


def meter_states(*, meter_ids) -> dict:
    """{meter_id: [ConditionState de reglas activas]} para pintar el estado de cada variable."""
    states: dict = {}
    for state in (
        ConditionState.objects.filter(meter_id__in=list(meter_ids), rule__is_active=True)
        .select_related("rule")
        .order_by("created_at")
    ):
        states.setdefault(state.meter_id, []).append(state)
    return states


# --- Sección "Condición" de la ficha del activo (CBM-07) ----------------------------

TREND_DAYS = 90
TABLE_LIMIT = 30  # filas de la tabla de datos bajo el gráfico (las más recientes)


class VariableStatus:
    ALARM = "alarm"
    NORMAL = "normal"
    NO_RULES = "no_rules"
    NO_READINGS = "no_readings"  # tiene reglas pero aún no hay lecturas que evaluar
    INACTIVE = "inactive"


@dataclass
class VariableCondition:
    """Una variable en la ficha: estado, reglas que le aplican y su tendencia de 90 días."""

    meter: Meter
    status: str
    rules: list = field(default_factory=list)
    points: list[TrendPoint] = field(default_factory=list)  # en orden de fecha
    active_alerts: int = 0
    since: datetime.datetime | None = None
    until: datetime.datetime | None = None

    @property
    def thresholds(self) -> list[Decimal]:
        """Líneas de umbral: el de `gt`/`lt` y los dos extremos de `outside` (`rate` no tiene)."""
        values = []
        for rule in self.rules:
            if rule.operator in (ConditionRule.Operator.GT, ConditionRule.Operator.LT):
                values.append(rule.threshold)
            elif rule.operator == ConditionRule.Operator.OUTSIDE:
                values += [rule.min_value, rule.max_value]
        return values

    @property
    def recent(self) -> list[TrendPoint]:
        """Las lecturas más recientes primero, para la tabla accesible."""
        return self.points[::-1][:TABLE_LIMIT]


def _out_of_range(rules, value) -> bool:
    """¿La lectura cruza algún umbral fijo? (`rate` depende de la anterior: no se marca)."""
    from .services import breaches

    return any(breaches(rule, value) for rule in rules if rule.operator != ConditionRule.Operator.RATE)


def asset_condition(*, membership, asset, now=None) -> list[VariableCondition]:
    """Variables del activo con su estado y tendencia de los últimos 90 días (CBM-07).

    `asset` ya es visible para la membresía (la vista lo obtiene con `visible_assets`).
    Incluye las desactivadas (su historial y alertas siguen visibles, CBM-01 CA5), al final.
    Consultas fijas, sin importar cuántas variables, reglas o lecturas haya: variables,
    reglas (del medidor y de la clase), estados, lecturas de la ventana y alertas activas.
    """
    require_perm(membership, Perm.CONDITION_VIEW)
    meters = list(
        Meter.objects.filter(asset_id=asset.pk, kind__in=VARIABLE_KINDS).order_by(
            "-is_active", "kind", "name"
        )
    )
    if not meters:
        return []
    ids = [meter.pk for meter in meters]
    until = now or timezone.now()
    since = until - datetime.timedelta(days=TREND_DAYS)
    rules = list(
        ConditionRule.objects.filter(is_active=True)
        .filter(
            Q(meter_id__in=ids)
            | Q(
                meter__isnull=True,
                asset_class_id=asset.asset_class_id,
                meter_kind__in={m.kind for m in meters},
            )
        )
        .order_by("created_at")
    )
    states = meter_states(meter_ids=ids)
    readings: dict = {}
    for meter_id, read_at, value in (
        MeterReading.objects.filter(meter_id__in=ids, read_at__gte=since)
        .order_by("read_at")
        .values_list("meter_id", "read_at", "value")
    ):
        readings.setdefault(meter_id, []).append((read_at, value))
    alerts = dict(
        ConditionAlert.objects.filter(meter_id__in=ids, status__in=ConditionAlert.ACTIVE_STATUSES)
        .values("meter_id")
        .annotate(n=Count("id"))
        .values_list("meter_id", "n")
    )
    rows = []
    for meter in meters:
        applicable = [
            rule
            for rule in rules
            if rule.meter_id == meter.pk
            or (rule.meter_id is None and rule.meter_kind == meter.kind and rule.meter_name == meter.name)
        ]
        rule_ids = {rule.pk for rule in applicable}
        in_alarm = any(
            state.status == ConditionState.Status.ALARM and state.rule_id in rule_ids
            for state in states.get(meter.pk, [])
        )
        if not meter.is_active:
            status = VariableStatus.INACTIVE
        elif not applicable:
            status = VariableStatus.NO_RULES
        elif meter.current_value is None:
            status = VariableStatus.NO_READINGS
        else:
            status = VariableStatus.ALARM if in_alarm else VariableStatus.NORMAL
        rows.append(
            VariableCondition(
                meter=meter,
                status=status,
                rules=applicable,
                points=[
                    TrendPoint(at=at, value=value, out_of_range=_out_of_range(applicable, value))
                    for at, value in readings.get(meter.pk, [])
                ],
                active_alerts=alerts.get(meter.pk, 0),
                since=since,
                until=until,
            )
        )
    return rows
