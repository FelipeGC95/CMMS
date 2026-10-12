"""Lecturas de inventario (INV-01/03/04): alcance por sitio y sin N+1."""

import datetime
import re
from dataclasses import dataclass, field
from decimal import Decimal

from django.db.models import (
    Case,
    Count,
    DecimalField,
    Exists,
    ExpressionWrapper,
    F,
    IntegerField,
    Max,
    OuterRef,
    Q,
    Subquery,
    Sum,
    Value,
    When,
)
from django.db.models.functions import Ceil, Coalesce, Greatest
from django.utils import timezone

from apps.accounts.permissions import Perm, has_perm, require_perm
from apps.accounts.scoping import allowed_location_ids
from apps.organizations.models import Location

from .models import Part, Reservation, StockItem, StockMovement, Warehouse

RECENT_MOVEMENTS = 20
STATUS_INACTIVE = "inactive"
STATUS_BELOW_MIN = "below_min"
STATUS_OUT = "out"
STATUS_NEGATIVE = "negative"
STATUS_NO_COST = "no_cost"
STOCK_STATUSES = ("", STATUS_INACTIVE, STATUS_BELOW_MIN, STATUS_OUT, STATUS_NEGATIVE, STATUS_NO_COST)
SEARCH_LIMIT = 20
ZERO = Value(Decimal(0), output_field=DecimalField(max_digits=14, decimal_places=3))


def warehouse_scope(membership) -> Q:
    """Bodegas que ve la membresía (INV-03 CA6): sin ubicación, o con ubicación en su alcance."""
    allowed = allowed_location_ids(membership)
    if allowed is None:
        return Q()
    return Q(location__isnull=True) | Q(location_id__in=allowed)


def visible_warehouses(*, membership, include_inactive: bool = False):
    """Bodegas de la organización que la membresía ve; activas salvo que se pidan todas."""
    require_perm(membership, Perm.INVENTORY_VIEW)
    qs = Warehouse.objects.filter(warehouse_scope(membership)).select_related("location")
    if not include_inactive:
        qs = qs.filter(is_active=True)
    return qs.order_by("-is_default", "name")


def can_see_warehouse(membership, warehouse) -> bool:
    allowed = allowed_location_ids(membership)
    return allowed is None or warehouse.location_id is None or warehouse.location_id in allowed


def default_warehouse():
    return Warehouse.objects.filter(is_default=True).first()


def _ancestor_chain(location_id) -> list:
    """La ubicación y sus ancestros, del más cercano al más lejano (una consulta)."""
    parents = dict(Location.objects.values_list("id", "parent_id"))
    chain, seen = [], set()
    while location_id is not None and location_id not in seen:
        chain.append(location_id)
        seen.add(location_id)
        location_id = parents.get(location_id)
    return chain


def warehouse_for(*, work_order=None, location=None, part=None):
    """Bodega de la que sale un repuesto para una OT (INV-09 CA3).

    1) la de la reserva activa de ese repuesto en la OT; 2) la bodega activa cuya
    ubicación es el ancestro más cercano de la ubicación del activo (o de la OT);
    3) la bodega por defecto (se crea si aún no existe, INV-03 CA2).
    """
    if work_order is not None and part is not None:
        reservation = (
            Reservation.objects.filter(
                work_order=work_order, part=part, status=Reservation.Status.ACTIVE, warehouse__is_active=True
            )
            .select_related("warehouse")
            .first()
        )
        if reservation is not None:
            return reservation.warehouse
    if location is None and work_order is not None:
        location_id = work_order.asset.location_id if work_order.asset_id else work_order.location_id
    else:
        location_id = getattr(location, "pk", location)
    if location_id is not None:
        by_location = {
            warehouse.location_id: warehouse
            for warehouse in Warehouse.objects.filter(is_active=True, location__isnull=False).order_by(
                "-is_default", "code"
            )
        }
        if by_location:
            for ancestor_id in _ancestor_chain(location_id):
                if ancestor_id in by_location:
                    return by_location[ancestor_id]
    from .services import ensure_default_warehouse

    return ensure_default_warehouse()


def stock_list(*, membership, q: str = "", category: str = "", warehouse=None, status: str = ""):
    """Repuestos con su stock total en las bodegas visibles (o en una), filtrados (INV-01, INV-12).

    Anota `on_hand_total`, `reserved_total`, `available_total` y `below_min`
    (alguna bodega bajo mínimo). Estados: "" activos, `inactive`, `below_min`,
    `out` (existencia total 0), `negative` (alguna bodega en negativo) y
    `no_cost` (con existencia y costo promedio 0, INV-08).
    """
    require_perm(membership, Perm.INVENTORY_VIEW)
    warehouses = Warehouse.objects.filter(warehouse_scope(membership))
    if warehouse is not None:
        warehouses = warehouses.filter(pk=warehouse.pk)
    in_scope = Q(stock_items__warehouse__in=warehouses)
    items = StockItem.objects.filter(part=OuterRef("pk"), warehouse__in=warehouses)
    qs = Part.objects.annotate(
        on_hand_total=Coalesce(Sum("stock_items__on_hand", filter=in_scope), ZERO),
        reserved_total=Coalesce(Sum("stock_items__reserved", filter=in_scope), ZERO),
        below_min=Exists(items.filter(min_quantity__gt=0, on_hand__lt=F("reserved") + F("min_quantity"))),
        negative=Exists(items.filter(on_hand__lt=0)),
    ).annotate(available_total=F("on_hand_total") - F("reserved_total"))
    qs = qs.filter(is_active=status != STATUS_INACTIVE)
    if q := q.strip():
        qs = qs.filter(Q(code__icontains=q) | Q(name__icontains=q) | Q(manufacturer_ref__icontains=q))
    if category := category.strip():
        qs = qs.filter(category__iexact=category)
    if status == STATUS_BELOW_MIN:
        qs = qs.filter(below_min=True)
    elif status == STATUS_OUT:
        qs = qs.filter(on_hand_total=0)
    elif status == STATUS_NEGATIVE:
        qs = qs.filter(negative=True)
    elif status == STATUS_NO_COST:
        qs = qs.filter(on_hand_total__gt=0, avg_cost=0)
    return qs.order_by("code")


FIELD_PART_LIMIT = 2000


def field_parts(*, membership) -> tuple[list, bool]:
    """Catálogo compacto para la PWA sin conexión (INV-13): activos, sin costos, tope 2000.

    Devuelve (filas, truncado). Vacío si el plan no incluye `inventory` o la membresía no
    tiene `inventory.view`: el teléfono solo ofrece texto libre. Una consulta.
    """
    from apps.organizations.modules import has_module

    if not has_module(membership.organization, "inventory") or not has_perm(membership, Perm.INVENTORY_VIEW):
        return [], False
    rows = list(
        Part.objects.filter(is_active=True)
        .order_by("code")
        .values("id", "code", "name", "unit", "category")[: FIELD_PART_LIMIT + 1]
    )
    return rows[:FIELD_PART_LIMIT], len(rows) > FIELD_PART_LIMIT


def stock_counters(*, membership, warehouse=None) -> dict:
    """Contadores del inicio de inventario (INV-12): repuestos activos bajo mínimo, en negativo y
    sin costo, en las bodegas visibles (o en una). Tres consultas, sin importar el volumen."""
    require_perm(membership, Perm.INVENTORY_VIEW)
    warehouses = Warehouse.objects.filter(warehouse_scope(membership))
    if warehouse is not None:
        warehouses = warehouses.filter(pk=warehouse.pk)
    items = StockItem.objects.filter(warehouse__in=warehouses, part__is_active=True)
    below = items.filter(min_quantity__gt=0, on_hand__lt=F("reserved") + F("min_quantity"))
    return {
        "below_min": below.values("part").distinct().count(),
        "negative": items.filter(on_hand__lt=0).values("part").distinct().count(),
        "no_cost": stock_list(membership=membership, warehouse=warehouse, status=STATUS_NO_COST).count(),
    }


def part_categories() -> list[str]:
    """Categorías en uso en repuestos activos (sugerencias y filtro, INV-01 CA1)."""
    return list(
        Part.objects.filter(is_active=True)
        .exclude(category="")
        .order_by("category")
        .values_list("category", flat=True)
        .distinct()
    )


def part_search(*, membership, q: str, limit: int = SEARCH_LIMIT) -> list:
    """Buscador de repuestos activos por código, nombre o n° de parte, con su disponible (INV-01 CA3)."""
    if not q.strip():
        return []
    return list(stock_list(membership=membership, q=q)[:limit])


@dataclass
class StockRow:
    """Una bodega en la ficha del repuesto: su saldo (o None si aún no hay movimientos)."""

    warehouse: Warehouse
    item: StockItem | None

    @property
    def on_hand(self) -> Decimal:
        return self.item.on_hand if self.item else Decimal(0)

    @property
    def reserved(self) -> Decimal:
        return self.item.reserved if self.item else Decimal(0)

    @property
    def available(self) -> Decimal:
        return self.on_hand - self.reserved

    @property
    def is_below_min(self) -> bool:
        return bool(self.item and self.item.is_below_min)


def stock_rows(detail, *, warehouses) -> list[StockRow]:
    """Stock por bodega de la ficha: las bodegas activas visibles y las inactivas que tienen saldo."""
    by_warehouse = {item.warehouse_id: item for item in detail.stock_items}
    rows = [StockRow(warehouse, by_warehouse.pop(warehouse.pk, None)) for warehouse in warehouses]
    rows += [StockRow(item.warehouse, item) for item in by_warehouse.values()]
    return rows


@dataclass
class PartDetail:
    part: Part
    stock_items: list
    movements: list
    show_costs: bool
    on_hand_total: Decimal = field(default=Decimal(0))
    reserved_total: Decimal = field(default=Decimal(0))

    @property
    def available_total(self) -> Decimal:
        return self.on_hand_total - self.reserved_total


def part_detail(pk, *, membership) -> PartDetail:
    """Ficha del repuesto (INV-04): stock por bodega visible y últimos movimientos.

    Lanza `Part.DoesNotExist` si no es de la organización. Los costos se
    muestran solo con `inventory.cost_view` (Q3).
    """
    require_perm(membership, Perm.INVENTORY_VIEW)
    part = Part.objects.get(pk=pk)
    scope = warehouse_scope(membership)
    stock_items = list(
        StockItem.objects.filter(part=part, warehouse__in=Warehouse.objects.filter(scope))
        .select_related("warehouse")
        .order_by("-warehouse__is_default", "warehouse__name")
    )
    movements = list(
        StockMovement.objects.filter(part=part, warehouse__in=Warehouse.objects.filter(scope))
        .select_related("warehouse", "work_order", "created_by")
        .order_by("-occurred_at", "-created_at")[:RECENT_MOVEMENTS]
    )
    return PartDetail(
        part=part,
        stock_items=stock_items,
        movements=movements,
        show_costs=has_perm(membership, Perm.INVENTORY_COST_VIEW),
        on_hand_total=sum((item.on_hand for item in stock_items), Decimal(0)),
        reserved_total=sum((item.reserved for item in stock_items), Decimal(0)),
    )


# --- Movimientos (INV-14) y reposición (INV-12) -------------------------------

WORK_ORDER_REF = re.compile(r"^(?:OT-)?(?:([A-Z])-)?0*(\d{1,9})$", re.IGNORECASE)


def _work_order_filter(text: str) -> Q | None:
    """«OT-C-00012», «C-12» o «12» → filtro por número (y tipo, si viene); None si no se entiende."""
    match = WORK_ORDER_REF.match(text.strip())
    if match is None:
        return None
    from apps.work.models import WorkOrder

    condition = Q(work_order__number=int(match.group(2)))
    if prefix := (match.group(1) or "").upper():
        types = [kind for kind, value in WorkOrder.TYPE_PREFIX.items() if value == prefix]
        condition &= Q(work_order__work_type__in=types)
    return condition


def movement_list(
    *,
    membership,
    part: str = "",
    warehouse=None,
    kind: str = "",
    date_from=None,
    date_to=None,
    work_order: str = "",
):
    """Libro de movimientos de las bodegas visibles, del más reciente al más antiguo (INV-14 CA1).

    `part`: código exacto del repuesto (sin mayúsculas) o, si no hay uno, parte del código o
    del nombre. `work_order`: código de la OT («OT-C-00012», «12»). Fechas locales inclusivas
    sobre la fecha real (`occurred_at`). Sin N+1: repuesto, bodega, OT y autor en la misma consulta.
    """
    require_perm(membership, Perm.INVENTORY_VIEW)
    qs = StockMovement.objects.filter(warehouse__in=Warehouse.objects.filter(warehouse_scope(membership)))
    if warehouse is not None:
        qs = qs.filter(warehouse=warehouse)
    if text := part.strip():
        exact = Part.objects.filter(code__iexact=text).values_list("pk", flat=True).first()
        if exact is not None:
            qs = qs.filter(part_id=exact)
        else:
            qs = qs.filter(Q(part__code__icontains=text) | Q(part__name__icontains=text))
    if kind:
        qs = qs.filter(kind=kind)
    if date_from is not None:
        qs = qs.filter(occurred_at__gte=_day_start(date_from))
    if date_to is not None:
        qs = qs.filter(occurred_at__lt=_day_start(date_to + datetime.timedelta(days=1)))
    if text := work_order.strip():
        condition = _work_order_filter(text)
        qs = qs.filter(condition) if condition is not None else qs.none()
    return qs.select_related("part", "warehouse", "work_order", "created_by").order_by(
        "-occurred_at", "-created_at"
    )


def _day_start(day):
    return timezone.make_aware(datetime.datetime.combine(day, datetime.time.min))


REORDER_NEGATIVE = 0
REORDER_UNCOVERED = 1
REORDER_BELOW_MIN = 2


def reorder_list(*, membership, warehouse=None):
    """Lista de reposición por bodega (INV-12 CA4): repuestos activos en negativo, con reservas sin
    cubrir o bajo mínimo, en las bodegas activas visibles (o en una).

    Anota `available_quantity`, `reservation_gap` (reservado que la existencia no cubre),
    `suggested` = max(objetivo, mínimo) − disponible redondeado hacia arriba y `priority`
    (0 negativo, 1 reservas sin cubrir, 2 bajo mínimo). Orden: bodega (la por defecto
    primero), prioridad y código. Una consulta, sin importar el volumen.
    """
    require_perm(membership, Perm.INVENTORY_VIEW)
    warehouses = Warehouse.objects.filter(warehouse_scope(membership), is_active=True)
    if warehouse is not None:
        warehouses = warehouses.filter(pk=warehouse.pk)
    quantity = DecimalField(max_digits=14, decimal_places=3)
    available = ExpressionWrapper(F("on_hand") - F("reserved"), output_field=quantity)
    covered = Greatest(F("on_hand"), ZERO, output_field=quantity)
    goal = Coalesce(F("target_quantity"), F("min_quantity"), output_field=quantity)
    return (
        StockItem.objects.filter(warehouse__in=warehouses, part__is_active=True)
        .annotate(available_quantity=available)
        .filter(
            Q(on_hand__lt=0)
            | Q(available_quantity__lt=0)
            | Q(min_quantity__gt=0, available_quantity__lt=F("min_quantity"))
        )
        .annotate(
            reservation_gap=Greatest(
                ExpressionWrapper(F("reserved") - covered, output_field=quantity), ZERO, output_field=quantity
            ),
            suggested=Ceil(ExpressionWrapper(goal - F("available_quantity"), output_field=quantity)),
            priority=Case(
                When(on_hand__lt=0, then=Value(REORDER_NEGATIVE)),
                When(reserved__gt=covered, then=Value(REORDER_UNCOVERED)),
                default=Value(REORDER_BELOW_MIN),
                output_field=IntegerField(),
            ),
        )
        .select_related("part", "warehouse")
        .order_by("-warehouse__is_default", "warehouse__name", "warehouse_id", "priority", "part__code")
    )


# --- "Dónde se usa" (INV-01 CA4) -----------------------------------------------

WHERE_USED_DAYS = 730  # 24 meses
WHERE_USED_LIMIT = 20


@dataclass
class PlanUse:
    """Un plan cuya versión vigente prevé el repuesto, con la cantidad por ejecución."""

    plan: object
    quantity: Decimal


@dataclass
class AssetUse:
    """Un activo donde se consumió el repuesto: cantidad neta y última fecha."""

    asset_id: object
    code: str
    name: str
    quantity: Decimal
    last_used_at: datetime.datetime


@dataclass
class WhereUsed:
    plans: list
    assets: list
    more_assets: bool = False


def part_where_used(part, *, membership, now=None) -> WhereUsed:
    """Dónde se usa el repuesto (INV-01 CA4), calculado y sin tablas nuevas (ADR-028 §9).

    Planes activos cuya versión vigente (publicada) lo prevé, con su clase de activo, y
    activos visibles (alcance por sitio) donde se consumió en los últimos 24 meses:
    cantidad neta (quitar una línea de la OT la devuelve y la borra) y última fecha.
    Dos consultas, sin importar el volumen.
    """
    from apps.maintenance.models import PlanResource
    from apps.work.models import PartUsage

    require_perm(membership, Perm.INVENTORY_VIEW)
    resources = (
        PlanResource.objects.filter(
            part=part, version__plan__current_version=F("version"), version__plan__is_active=True
        )
        .select_related("version__plan__asset_class")
        .order_by("version__plan__name", "version__plan_id")
    )
    plans: dict = {}
    for resource in resources:
        plan = resource.version.plan
        if plan.pk in plans:
            plans[plan.pk].quantity += resource.quantity
        else:
            plans[plan.pk] = PlanUse(plan=plan, quantity=resource.quantity)

    since = (now or timezone.now()) - datetime.timedelta(days=WHERE_USED_DAYS)
    usages = PartUsage.objects.filter(part=part, work_order__asset__isnull=False).annotate(
        used_at=Coalesce("stock_movement__occurred_at", "created_at")
    )
    allowed = allowed_location_ids(membership)
    if allowed is not None:
        usages = usages.filter(work_order__asset__location_id__in=allowed)
    rows = list(
        usages.filter(used_at__gte=since)
        .values("work_order__asset_id", "work_order__asset__code", "work_order__asset__name")
        .annotate(total=Sum("quantity"), last=Max("used_at"))
        .order_by("-last", "work_order__asset__code")[: WHERE_USED_LIMIT + 1]
    )
    assets = [
        AssetUse(
            asset_id=row["work_order__asset_id"],
            code=row["work_order__asset__code"],
            name=row["work_order__asset__name"],
            quantity=row["total"],
            last_used_at=row["last"],
        )
        for row in rows[:WHERE_USED_LIMIT]
    ]
    return WhereUsed(plans=list(plans.values()), assets=assets, more_assets=len(rows) > WHERE_USED_LIMIT)


# --- Indicadores (INV-15) -------------------------------------------------------

INSIGHTS_DAYS = 365  # 12 meses
STALE_LIMIT = 10
MONEY_STEP = Decimal("0.01")
VALUE_FIELD = DecimalField(max_digits=32, decimal_places=7)


def _money(value) -> Decimal:
    return (value or Decimal(0)).quantize(MONEY_STEP)


@dataclass
class WarehouseValue:
    warehouse_id: object
    code: str
    name: str
    value: Decimal


def inventory_value(*, membership) -> tuple[Decimal, list]:
    """Valor del inventario (INV-15): Σ on_hand positivo × costo promedio, total y por bodega.

    Solo bodegas visibles (activas o no, si tienen saldo). Devuelve
    (total, [WarehouseValue]) con las bodegas de mayor valor primero. Una consulta.
    """
    require_perm(membership, Perm.INVENTORY_COST_VIEW)
    value = ExpressionWrapper(F("on_hand") * F("part__avg_cost"), output_field=VALUE_FIELD)
    rows = list(
        StockItem.objects.filter(
            warehouse__in=Warehouse.objects.filter(warehouse_scope(membership)), on_hand__gt=0
        )
        .values("warehouse_id", "warehouse__code", "warehouse__name")
        .annotate(value=Sum(value))
        .order_by("-value", "warehouse__name")
    )
    by_warehouse = [
        WarehouseValue(
            warehouse_id=row["warehouse_id"],
            code=row["warehouse__code"],
            name=row["warehouse__name"],
            value=_money(row["value"]),
        )
        for row in rows
    ]
    return sum((row.value for row in by_warehouse), Decimal(0)), by_warehouse


def consumption_12m(*, membership, now=None) -> Decimal:
    """Consumo valorizado de los últimos 12 meses (INV-15): salidas a OT − devoluciones.

    Al costo con que quedó cada movimiento, en las bodegas visibles. Una consulta.
    """
    require_perm(membership, Perm.INVENTORY_COST_VIEW)
    since = (now or timezone.now()) - datetime.timedelta(days=INSIGHTS_DAYS)
    total = StockMovement.objects.filter(
        warehouse__in=Warehouse.objects.filter(warehouse_scope(membership)),
        kind__in=[StockMovement.Kind.ISSUE, StockMovement.Kind.RETURN],
        occurred_at__gte=since,
    ).aggregate(total=Sum("total_cost"))["total"]
    # Las salidas tienen costo negativo y las devoluciones positivo: el consumo es el opuesto.
    return _money(-(total or Decimal(0)))


def stale_parts(*, membership, now=None):
    """Repuestos con existencia y sin movimientos en 12 meses (INV-15), con su valor.

    Anota `stock_value` (Σ on_hand positivo × costo promedio en las bodegas visibles) y
    `last_moved_at`; los de mayor valor primero. Una consulta.
    """
    require_perm(membership, Perm.INVENTORY_COST_VIEW)
    since = (now or timezone.now()) - datetime.timedelta(days=INSIGHTS_DAYS)
    warehouses = Warehouse.objects.filter(warehouse_scope(membership))
    movements = StockMovement.objects.filter(part=OuterRef("pk"), warehouse__in=warehouses)
    in_scope = Q(stock_items__warehouse__in=warehouses, stock_items__on_hand__gt=0)
    return (
        Part.objects.annotate(
            stock_value=Sum(
                ExpressionWrapper(F("stock_items__on_hand") * F("avg_cost"), output_field=VALUE_FIELD),
                filter=in_scope,
            ),
            last_moved_at=Subquery(movements.order_by("-occurred_at").values("occurred_at")[:1]),
        )
        .filter(stock_value__gt=0)
        .exclude(Exists(movements.filter(occurred_at__gte=since)))
        .order_by("-stock_value", "code")
    )


@dataclass
class InventoryInsights:
    total_value: Decimal
    by_warehouse: list
    consumption: Decimal
    stale_count: int
    stale_value: Decimal
    stale: list

    @property
    def turnover(self) -> Decimal | None:
        """Rotación aproximada: consumo de 12 meses ÷ valor actual (vacía si no hay valor)."""
        if self.total_value <= 0:
            return None
        return (self.consumption / self.total_value).quantize(Decimal("0.1"))


def inventory_insights(*, membership, now=None) -> InventoryInsights:
    """Los cuatro indicadores del inicio de inventario (INV-15) en cuatro consultas fijas.

    `stale` trae los 10 repuestos sin movimiento de mayor valor, como (repuesto, valor).
    """
    total, by_warehouse = inventory_value(membership=membership)
    stale = stale_parts(membership=membership, now=now)
    summary = stale.aggregate(count=Count("pk"), value=Sum("stock_value"))
    return InventoryInsights(
        total_value=total,
        by_warehouse=by_warehouse,
        consumption=consumption_12m(membership=membership, now=now),
        stale_count=summary["count"] or 0,
        stale_value=_money(summary["value"]),
        stale=[(part, _money(part.stock_value)) for part in stale[:STALE_LIMIT]],
    )
