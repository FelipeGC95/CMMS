"""Lecturas de inventario (INV-01/03/04): alcance por sitio y sin N+1."""

from dataclasses import dataclass, field
from decimal import Decimal

from django.db.models import DecimalField, Exists, F, OuterRef, Q, Sum, Value
from django.db.models.functions import Coalesce

from apps.accounts.permissions import Perm, has_perm, require_perm
from apps.accounts.scoping import allowed_location_ids
from apps.organizations.models import Location

from .models import Part, Reservation, StockItem, StockMovement, Warehouse

RECENT_MOVEMENTS = 20
STATUS_INACTIVE = "inactive"
STATUS_BELOW_MIN = "below_min"
STATUS_OUT = "out"
STATUS_NEGATIVE = "negative"
STOCK_STATUSES = ("", STATUS_INACTIVE, STATUS_BELOW_MIN, STATUS_OUT, STATUS_NEGATIVE)
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
    `out` (existencia total 0), `negative` (alguna bodega en negativo).
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
    return qs.order_by("code")


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
