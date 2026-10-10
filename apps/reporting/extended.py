"""Indicadores ampliados (KPI-02). Definiciones en docs/02_requisitos.md §8.

Cada indicador recibe la membresía y los filtros del tablero (`KpiFilters`) y
usa el mismo alcance que KPI-01 (`kpi_scope`): sitios permitidos de la
membresía y filtros de fecha, sitio, clase, criticidad y activo. Todos se
calculan con un número fijo de consultas agregadas en la base de datos.
"""

import datetime
from dataclasses import dataclass, field
from decimal import Decimal

from django.db.models import (
    Case,
    Count,
    DateTimeField,
    DurationField,
    F,
    FilteredRelation,
    Q,
    Sum,
    Value,
    When,
)
from django.db.models.functions import Coalesce, Least, TruncMonth
from django.utils import timezone

from apps.work.models import FailureEvent, WorkOrder
from apps.work.selectors import OPEN_STATUSES, technicians_in_scope

from .selectors import KpiFilters, kpi_scope

# Semanas que mide el backlog cuando no se elige otro período (CA1).
DEFAULT_BACKLOG_WEEKS = 12
# Tope de puntos de la serie de backlog: un año de semanas.
MAX_BACKLOG_POINTS = 53
# Gráfico de columnas: cada semana ocupa 10 unidades del viewBox (columna de 7 y aire de 3).
COLUMN_STEP = 10

# Proporción preventivo/correctivo (CA2): trabajo planificado frente a correctivo.
PLANNED_TYPES = (WorkOrder.Type.PREVENTIVE, WorkOrder.Type.PREDICTIVE, WorkOrder.Type.INSPECTION)
CORRECTIVE_TYPES = (WorkOrder.Type.CORRECTIVE,)

# Activos que se muestran en el ranking de costos antes de «Ver todos» (CA3).
TOP_COST_ASSETS = 10

# Pareto de fallas (CA4).
PARETO_THRESHOLD = 80
PARETO_CODES = ("mode", "cause")
PARETO_METRICS = ("count", "downtime")


def _hours(minutes) -> Decimal:
    return (Decimal(minutes or 0) / 60).quantize(Decimal("0.1"))


def _share(part, whole) -> float | None:
    return 100 * float(part) / float(whole) if whole else None


# --- CA1 · Backlog en semanas en el tiempo -------------------------------------------


@dataclass
class BacklogPoint:
    at: datetime.datetime  # instante medido (fin del día `day`)
    day: datetime.date  # día que cierra la semana, en la zona activa
    count: int
    estimated_hours: Decimal
    unestimated: int
    weeks: Decimal | None
    bar_pct: float = 0.0  # alto de la barra (0–100) respecto del punto mayor
    x: float = 0.0  # posición de la columna en el gráfico (unidades del viewBox)

    @property
    def y(self) -> float:
        return round(100 - self.bar_pct, 1)


@dataclass
class BacklogTrend:
    points: list
    weekly_capacity_hours: Decimal
    technicians: int

    @property
    def current(self) -> BacklogPoint | None:
        return self.points[-1] if self.points else None

    @property
    def chart_width(self) -> int:
        """Ancho del viewBox del gráfico de columnas: 10 unidades por semana."""
        return len(self.points) * COLUMN_STEP

    @property
    def by_hours(self) -> bool:
        """Sin capacidad las barras muestran horas: las semanas no se pueden calcular."""
        return not self.weekly_capacity_hours


def backlog_instants(window_start, window_end) -> list:
    """Cortes semanales hacia atrás desde el fin de la ventana: fin, fin − 7 días, … (> inicio)."""
    points = []
    at = window_end
    while at > window_start and len(points) < MAX_BACKLOG_POINTS:
        points.append(at)
        at -= datetime.timedelta(days=7)
    return points[::-1]


def left_backlog_at():
    """Instante en que la OT dejó el backlog: término, cierre o cancelación (None si sigue abierta).

    La cancelación se toma de su cambio de estado (WO-02 CA3); una OT cancelada
    sin ese registro usa su última modificación. Requiere la relación filtrada
    `cancel_transition` (ver `backlog_trend`).
    """
    return Case(
        When(status__in=OPEN_STATUSES, then=Value(None, output_field=DateTimeField())),
        default=Coalesce("completed_at", "closed_at", "cancel_transition__created_at", "updated_at"),
        output_field=DateTimeField(),
    )


def backlog_trend(*, membership, filters: KpiFilters, now=None) -> BacklogTrend:
    """Backlog en semanas reconstruido a cada corte semanal (KPI-02 CA1).

    Misma definición que REQ-04 CA2: horas estimadas de las OT pendientes (ni
    completadas, ni cerradas, ni canceladas) / capacidad semanal de los técnicos
    del alcance (`technicians_in_scope`). Una OT estaba pendiente en el instante
    `t` si se creó a más tardar en `t` y aún no salía del backlog
    (`left_backlog_at`). La capacidad es la vigente (no hay historial de
    capacidad): la serie muestra cómo cambió la carga con el equipo de hoy.
    Una sola consulta agregada para toda la serie.
    """
    now = now or timezone.now()
    window_start, window_end = filters.window(now)
    instants = backlog_instants(window_start, window_end)
    technicians = technicians_in_scope(membership=membership, location=filters.location)
    capacity = sum((t.weekly_capacity_hours for t in technicians), Decimal(0))
    if not instants:
        return BacklogTrend([], capacity, len(technicians))

    qs = (
        kpi_scope(membership=membership, filters=filters)
        .work_orders.order_by()
        .annotate(
            cancel_transition=FilteredRelation(
                "transitions", condition=Q(transitions__to_status=WorkOrder.Status.CANCELLED)
            )
        )
        .annotate(left_at=left_backlog_at())
        .filter(created_at__lte=instants[-1])
        .filter(Q(left_at__isnull=True) | Q(left_at__gt=instants[0]))
    )
    aggregates = {}
    for index, at in enumerate(instants):
        pending = Q(created_at__lte=at) & (Q(left_at__isnull=True) | Q(left_at__gt=at))
        aggregates[f"n{index}"] = Count("pk", filter=pending)
        aggregates[f"m{index}"] = Sum("estimated_minutes", filter=pending)
        aggregates[f"u{index}"] = Count("pk", filter=pending & Q(estimated_minutes__isnull=True))
    totals = qs.aggregate(**aggregates)

    points = []
    for index, at in enumerate(instants):
        estimated = _hours(totals[f"m{index}"])
        points.append(
            BacklogPoint(
                at=at,
                day=timezone.localdate(at - datetime.timedelta(microseconds=1)),
                count=totals[f"n{index}"],
                estimated_hours=estimated,
                unestimated=totals[f"u{index}"],
                weeks=(estimated / capacity).quantize(Decimal("0.1")) if capacity else None,
            )
        )
    peak = max(p.estimated_hours for p in points)
    for index, p in enumerate(points):
        p.bar_pct = round(float(100 * p.estimated_hours / peak), 1) if peak else 0.0
        p.x = index * COLUMN_STEP + 1.5
    return BacklogTrend(points, capacity, len(technicians))


# --- CA2 · Proporción preventivo/correctivo -------------------------------------------


@dataclass
class MixRow:
    month: datetime.date | None  # None en la fila de totales
    planned_count: int = 0
    corrective_count: int = 0
    other_count: int = 0
    planned_minutes: int = 0
    corrective_minutes: int = 0
    other_minutes: int = 0

    @property
    def planned_hours(self) -> Decimal:
        return _hours(self.planned_minutes)

    @property
    def corrective_hours(self) -> Decimal:
        return _hours(self.corrective_minutes)

    @property
    def other_hours(self) -> Decimal:
        return _hours(self.other_minutes)

    @property
    def planned_pct(self) -> float | None:
        """% planificado por horas (SMRP): horas planificadas / (planificadas + correctivas)."""
        return _share(self.planned_minutes, self.planned_minutes + self.corrective_minutes)

    @property
    def planned_pct_count(self) -> float | None:
        """% planificado por cantidad de OT."""
        return _share(self.planned_count, self.planned_count + self.corrective_count)

    @property
    def bar_planned(self) -> float:
        """Ancho (0–100) de la parte planificada de la barra: por horas, o por cantidad si no hay horas."""
        pct = self.planned_pct if self.planned_pct is not None else self.planned_pct_count
        return round(pct, 1) if pct is not None else 0.0

    @property
    def bar_corrective(self) -> float:
        return round(100 - self.bar_planned, 1) if self.has_data else 0.0

    @property
    def has_data(self) -> bool:
        return bool(self.planned_count + self.corrective_count)


@dataclass
class WorkMix:
    months: list
    total: MixRow = field(default_factory=lambda: MixRow(None))


def _months(start: datetime.date, end: datetime.date) -> list:
    months, month = [], start.replace(day=1)
    while month <= end:
        months.append(month)
        month = (month + datetime.timedelta(days=32)).replace(day=1)
    return months


def work_mix(*, membership, filters: KpiFilters, now=None) -> WorkMix:
    """OT completadas en el período por tipo y mes: planificado vs correctivo (KPI-02 CA2).

    Planificado = preventiva + predictiva/condición + inspección; correctivo =
    correctiva; las de mejora se informan aparte y no entran al porcentaje.
    Horas = horas registradas en la OT (WO-04 CA1). Cuenta la OT completada en la
    ventana (por su fecha de término) que no terminó cancelada. Una consulta.
    """
    now = now or timezone.now()
    window_start, window_end = filters.window(now)
    rows = (
        kpi_scope(membership=membership, filters=filters)
        .work_orders.filter(completed_at__gte=window_start, completed_at__lt=window_end)
        .exclude(status=WorkOrder.Status.CANCELLED)
        .annotate(month=TruncMonth("completed_at"))
        .values("month", "work_type")
        .annotate(n=Count("pk", distinct=True), minutes=Sum("labor__minutes"))
        .order_by()
    )
    first = timezone.localdate(window_start)
    last = timezone.localdate(window_end - datetime.timedelta(microseconds=1))
    by_month = {month: MixRow(month) for month in _months(first, last)}
    total = MixRow(None)
    for row in rows:
        month = row["month"]
        month = (timezone.localdate(month) if isinstance(month, datetime.datetime) else month).replace(day=1)
        target = by_month.setdefault(month, MixRow(month))
        kind = (
            "planned"
            if row["work_type"] in PLANNED_TYPES
            else "corrective"
            if row["work_type"] in CORRECTIVE_TYPES
            else "other"
        )
        for item in (target, total):
            setattr(item, f"{kind}_count", getattr(item, f"{kind}_count") + row["n"])
            setattr(item, f"{kind}_minutes", getattr(item, f"{kind}_minutes") + (row["minutes"] or 0))
    return WorkMix(months=[by_month[m] for m in sorted(by_month)], total=total)


# --- CA3 · Costos por activo y por clase -----------------------------------------------

COST_SUMS = {
    "labor": Sum("labor_cost"),
    "parts": Sum("parts_cost"),
    "other": Sum("other_cost"),
    "total": Sum("total_cost"),
    "work_orders": Count("pk"),
}


@dataclass
class CostReport:
    by_asset: object  # consulta agregada por activo, mayor costo primero
    by_class: list
    totals: dict
    location_totals: dict  # OT cerradas sobre una ubicación (sin activo)


def closed_in_window(*, membership, filters: KpiFilters, now=None):
    now = now or timezone.now()
    window_start, window_end = filters.window(now)
    return kpi_scope(membership=membership, filters=filters).work_orders.filter(
        status=WorkOrder.Status.CLOSED, closed_at__gte=window_start, closed_at__lt=window_end
    )


def cost_report(*, membership, filters: KpiFilters, now=None) -> CostReport:
    """Costos de las OT cerradas en el período por activo y por clase (KPI-02 CA3).

    Usa los totales guardados en la OT al cerrar (WO-04 CA3): mano de obra,
    repuestos, otros y total. Cada OT suma a su activo directo (un componente es
    su propia fila; no se suma al padre, así nada cuenta dos veces). Las OT sobre
    una ubicación no tienen activo ni clase: van en una fila aparte. `by_asset`
    es una consulta perezosa (se pagina o se corta en la vista); clase y totales,
    una consulta cada una.
    """
    closed = closed_in_window(membership=membership, filters=filters, now=now).order_by()
    on_assets = closed.filter(asset__isnull=False)
    by_asset = (
        on_assets.values("asset_id", "asset__code", "asset__name", "asset__asset_class__name")
        .annotate(**COST_SUMS)
        .order_by("-total", "asset__code")
    )
    by_class = list(
        on_assets.values("asset__asset_class_id", "asset__asset_class__name")
        .annotate(assets=Count("asset_id", distinct=True), **COST_SUMS)
        .order_by("-total", "asset__asset_class__name")
    )
    zero = Decimal("0.00")
    sums = closed.aggregate(
        labor=Coalesce(Sum("labor_cost"), zero),
        parts=Coalesce(Sum("parts_cost"), zero),
        other=Coalesce(Sum("other_cost"), zero),
        total=Coalesce(Sum("total_cost"), zero),
        work_orders=Count("pk"),
        loc_labor=Coalesce(Sum("labor_cost", filter=Q(asset__isnull=True)), zero),
        loc_parts=Coalesce(Sum("parts_cost", filter=Q(asset__isnull=True)), zero),
        loc_other=Coalesce(Sum("other_cost", filter=Q(asset__isnull=True)), zero),
        loc_total=Coalesce(Sum("total_cost", filter=Q(asset__isnull=True)), zero),
        loc_work_orders=Count("pk", filter=Q(asset__isnull=True)),
    )
    totals = {k: v for k, v in sums.items() if not k.startswith("loc_")}
    location_totals = {k.removeprefix("loc_"): v for k, v in sums.items() if k.startswith("loc_")}
    by_class = with_bars(by_class, by_class[0]["total"] if by_class else 0)
    return CostReport(by_asset, by_class, totals, location_totals)


def with_bars(rows, peak) -> list:
    """Agrega `bar_pct` (0–100) a filas de costo respecto de `peak`."""
    rows = list(rows)
    for row in rows:
        row["bar_pct"] = round(float(100 * row["total"] / peak), 1) if peak else 0.0
    return rows


# --- CA4 · Pareto de fallas -------------------------------------------------------------


@dataclass
class ParetoRow:
    code: str
    name: str
    count: int
    downtime_hours: float
    value: float  # métrica elegida (cantidad u horas de detención)
    share_pct: float
    cumulative_pct: float
    vital: bool  # dentro del 80 %: la fila que cruza el 80 % también cuenta

    @property
    def before_pct(self) -> float:
        """% acumulado por las filas anteriores: dónde empieza el tramo de esta fila en la barra."""
        return round(max(self.cumulative_pct - self.share_pct, 0.0), 1)

    @property
    def share_bar(self) -> float:
        return round(self.share_pct, 1)


@dataclass
class Pareto:
    code_kind: str  # "mode" o "cause"
    metric: str  # "count" o "downtime"
    rows: list
    total_count: int
    total_downtime_hours: float
    uncoded_count: int
    uncoded_downtime_hours: float

    @property
    def vital_count(self) -> int:
        return sum(1 for r in self.rows if r.vital)


def _as_hours(value) -> float:
    if value is None:
        return 0.0
    if isinstance(value, datetime.timedelta):
        return value.total_seconds() / 3600
    return float(value) / 3_600_000_000  # microsegundos (SQLite)


def failure_pareto(
    *, membership, filters: KpiFilters, code_kind: str = "mode", metric: str = "count", now=None
) -> Pareto:
    """Pareto de fallas por modo o causa (KPI-02 CA4), con cantidad, detención y % acumulado.

    Cuenta las fallas iniciadas en la ventana sobre los activos del alcance.
    Detención = horas de las fallas con detención total (como MTTR, KPI-01),
    hasta su término o el fin de la ventana si siguen abiertas. Ordenado de mayor
    a menor por la métrica elegida; las fallas sin código quedan fuera del
    Pareto y se informan aparte. Una consulta agrupada.
    """
    code_kind = code_kind if code_kind in PARETO_CODES else "mode"
    metric = metric if metric in PARETO_METRICS else "count"
    now = now or timezone.now()
    window_start, window_end = filters.window(now)
    relation = "failure_mode" if code_kind == "mode" else "failure_cause"
    end = Value(window_end, output_field=DateTimeField())
    rows = (
        kpi_scope(membership=membership, filters=filters)
        .failures.filter(started_at__gte=window_start, started_at__lt=window_end)
        .values(f"{relation}_id", f"{relation}__code", f"{relation}__name")
        .annotate(
            n=Count("pk"),
            downtime=Sum(
                Least(Coalesce("ended_at", end), end) - F("started_at"),
                filter=Q(impact=FailureEvent.Impact.TOTAL_STOP),
                output_field=DurationField(),
            ),
        )
        .order_by()
    )
    coded, uncoded_count, uncoded_hours = [], 0, 0.0
    for row in rows:
        hours = _as_hours(row["downtime"])
        if row[f"{relation}_id"] is None:
            uncoded_count, uncoded_hours = row["n"], hours
        else:
            coded.append((row[f"{relation}__code"], row[f"{relation}__name"], row["n"], hours))
    position = 2 if metric == "count" else 3
    coded.sort(key=lambda r: (-r[position], r[0]))
    total_count = sum(r[2] for r in coded)
    total_hours = sum(r[3] for r in coded)
    total = total_count if metric == "count" else total_hours
    result, running = [], 0.0
    for code, name, count, hours in coded:
        value = count if metric == "count" else hours
        before = 100 * running / total if total else 0.0
        running += value
        result.append(
            ParetoRow(
                code=code,
                name=name,
                count=count,
                downtime_hours=hours,
                value=value,
                share_pct=100 * value / total if total else 0.0,
                cumulative_pct=100 * running / total if total else 0.0,
                vital=bool(total) and value > 0 and before < PARETO_THRESHOLD,
            )
        )
    return Pareto(code_kind, metric, result, total_count, total_hours, uncoded_count, uncoded_hours)
