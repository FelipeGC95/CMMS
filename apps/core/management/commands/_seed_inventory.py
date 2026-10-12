"""Inventario de la demo (INV, P7): bodegas, catálogo, stock, mínimos y fechas en el pasado.

Lo usa `seed_demo`. Solo datos de demostración: las fechas de los movimientos se
mueven al pasado directamente en la base (el libro es inmutable para la app), en el
mismo orden en que se crearon, para que el saldo resultante de cada movimiento siga
la secuencia cronológica.
"""

import datetime
from decimal import Decimal

from django.db.models import Max
from django.utils import timezone

from apps.inventory import services as inventory
from apps.inventory.models import Part, Reservation, StockItem, StockMovement

# Bodega principal (por defecto) en el área de bodega; un pañol por sitio.
MAIN, SITE = "BOD", "PLT"

# código, nombre, categoría, unidad, costo, bodega, cantidad inicial, mínimo, objetivo,
# días desde la entrada (None = antes del historial; más de 365 = sin movimiento en 12 meses).
PARTS = [
    ("REP-GRH-001", "Filtro de aceite motor grúa", "Filtros", "un", 8900, MAIN, 6, 2, 6, None),
    ("REP-GRH-002", "Filtro de aire grúa", "Filtros", "un", 14500, MAIN, 4, 2, 4, None),
    ("REP-GRH-003", "Aceite de motor 15W-40", "Lubricantes", "L", 7800, MAIN, 24, 8, 32, None),
    ("REP-GRH-004", "Aceite hidráulico 20 L", "Lubricantes", "bidón", 89000, MAIN, 4, 2, 4, None),
    ("REP-GRH-005", "Filtro de retorno hidráulico", "Filtros", "un", 21500, MAIN, 2, 1, 2, None),
    ("REP-GRH-006", "Kit de sellos de cilindro", "Sellos", "kit", 76000, MAIN, 2, 1, 2, None),
    ("REP-GRH-007", "Relé de control hidráulico", "Eléctricos", "un", 38000, MAIN, 2, 0, None, None),
    ("REP-VEH-001", "Filtro de aceite Hilux", "Filtros", "un", 12000, MAIN, 3, 1, 3, None),
    ("REP-VEH-002", "Aceite 5W-30 sintético", "Lubricantes", "L", 9500, MAIN, 14, 7, 14, None),
    ("REP-VEH-003", "Pastillas de freno delanteras Hilux", "Frenos", "juego", 45000, MAIN, 1, 2, 2, None),
    ("REP-GEN-002", "Batería 12 V 150 Ah", "Eléctricos", "un", 135000, MAIN, 2, 0, None, 395),
    ("REP-CMP-001", "Filtro de aire GA22", "Filtros", "un", 32000, SITE, 4, 2, 4, None),
    ("REP-CMP-002", "Filtro de aceite GA22", "Filtros", "un", 28000, SITE, 2, 1, 2, None),
    ("REP-CMP-003", "Aceite Roto-Inject 5 L", "Lubricantes", "bidón", 48000, SITE, 5, 2, 4, None),
    ("REP-CMP-004", "Juego de sellos enfriador", "Sellos", "kit", 145000, SITE, 2, 0, None, None),
    ("REP-CMP-005", "Separador aire/aceite GA22", "Filtros", "un", 210000, SITE, 1, 0, None, 430),
    ("REP-CAL-001", "Electrodo de nivel", "Caldera", "un", 56000, SITE, 2, 1, 2, None),
    ("REP-CAL-002", "Electrodo de ignición", "Caldera", "un", 28000, SITE, 3, 1, 3, None),
    ("REP-CAL-003", "Válvula de seguridad 1\" 10 bar", "Caldera", "un", 185000, SITE, 1, 0, None, 510),
    ("REP-CAL-004", "Empaquetadura tapa de inspección", "Sellos", "un", 22000, SITE, 3, 1, 2, None),
    ("REP-ENV-001", "Resistencia de mordaza", "Envasado", "un", 41000, SITE, 6, 2, 4, None),
    ("REP-ENV-002", "Teflón de mordaza", "Envasado", "rollo", 12500, SITE, 8, 4, 12, None),
    ("REP-ENV-003", "Cuchilla de corte", "Envasado", "un", 92000, SITE, 2, 1, 2, None),
    ("REP-ENV-005", "Correa dentada HTD 8M", "Transmisión", "un", 35000, SITE, 2, 1, 2, None),
    ("REP-FRI-001", "Contactor 3 polos 40 A", "Eléctricos", "un", 64000, SITE, 2, 1, 2, None),
    ("REP-FRI-002", "Presostato de alta", "Frío", "un", 87000, SITE, 2, 1, 1, None),
    ("REP-FRI-003", "Filtro deshidratador 3/8\"", "Frío", "un", 18000, SITE, 4, 2, 4, None),
    ("REP-FRI-004", "Refrigerante R404A", "Frío", "kg", 24000, SITE, 10, 5, 10, None),
    ("REP-GEN-001", "Filtro de petróleo Cummins", "Filtros", "un", 16000, SITE, 2, 1, 2, None),
]  # fmt: skip

# Rodamientos: se compran en la bodega principal y se llevan al pañol (una transferencia).
BEARING = ("REP-ENV-004", "Rodamiento 6205-2RS", "Rodamientos", "un", 9500, 10, 6)

# Repuestos previstos en los planes (INV-11): código del plan → [(código del repuesto, cantidad)].
PLAN_PARTS = {
    "PM-ENV-QUI": [("REP-ENV-002", 1)],
    "PM-CAL-TRI": [("REP-CAL-004", 1)],
    "PM-GRH-250": [("REP-GRH-001", 1), ("REP-GRH-002", 1), ("REP-GRH-003", 8)],
    "PM-GRH-500": [("REP-GRH-004", 2), ("REP-GRH-005", 1)],
    "PM-VEH-10K": [("REP-VEH-001", 1), ("REP-VEH-002", 7)],
}


def _at(days_ago, hour=9):
    moment = timezone.localtime() - datetime.timedelta(days=days_ago)
    return moment.replace(hour=hour, minute=0, second=0, microsecond=0)


def backdate_movements(queryset, moment):
    """Mueve movimientos al pasado (solo demo: el libro no se edita desde la app)."""
    StockMovement._base_manager.filter(pk__in=list(queryset.values_list("pk", flat=True))).update(
        occurred_at=moment
    )


def seed_warehouses_and_parts(*, admin, locations, history_days) -> dict:
    """Bodegas, catálogo y stock inicial con fechas anteriores al historial. Devuelve {código: repuesto}."""
    main = inventory.create_warehouse(
        membership=admin, code=MAIN, name="Bodega principal", location=locations["BOD"]
    )
    site = inventory.create_warehouse(
        membership=admin, code=SITE, name="Pañol Planta Osorno", location=locations["PLT"]
    )
    warehouses = {MAIN: main, SITE: site}
    before_history = history_days + 10
    parts = {}
    for code, name, category, unit, cost, home, quantity, minimum, target, days_ago in PARTS:
        part = inventory.create_part(membership=admin, code=code, name=name, category=category, unit=unit)
        movement = inventory.receive_stock(
            membership=admin,
            part=part,
            quantity=quantity,
            unit_cost=cost,
            warehouse=warehouses[home],
            reference=f"FAC-{1000 + len(parts)}",
            supplier="Distribuidora Sur" if home == MAIN else "Repuestos Industriales Ltda.",
        )
        backdate_movements(StockMovement.objects.filter(pk=movement.pk), _at(days_ago or before_history))
        if minimum or target:
            inventory.set_stock_levels(
                membership=admin,
                part=part,
                warehouse=warehouses[home],
                min_quantity=minimum,
                target_quantity=target,
                bin_location=f"{category[:3].upper()}-{len(parts) % 6 + 1:02d}",
            )
        parts[code] = part
    code, name, category, unit, cost, quantity, moved = BEARING
    bearing = inventory.create_part(membership=admin, code=code, name=name, category=category, unit=unit)
    receipt = inventory.receive_stock(
        membership=admin,
        part=bearing,
        quantity=quantity,
        unit_cost=cost,
        warehouse=main,
        reference="FAC-1099",
    )
    backdate_movements(StockMovement.objects.filter(pk=receipt.pk), _at(before_history))
    out, into = inventory.transfer_stock(
        membership=admin, part=bearing, from_warehouse=main, to_warehouse=site, quantity=moved
    )
    backdate_movements(StockMovement.objects.filter(pk__in=[out.pk, into.pk]), _at(before_history - 2, 15))
    inventory.set_stock_levels(
        membership=admin, part=bearing, warehouse=site, min_quantity=4, target_quantity=8
    )
    parts[code] = bearing
    return parts


def plan_resources(plan_code, parts) -> list:
    """Recursos de un plan enlazados al catálogo (formato de `create_plan`)."""
    return [
        {
            "kind": "part",
            "description": parts[code].name,
            "quantity": Decimal(quantity),
            "unit": parts[code].unit,
            "part": parts[code],
        }
        for code, quantity in PLAN_PARTS.get(plan_code, [])
    ]


def planned_usage(work_order) -> list:
    """Repuestos previstos pendientes de una OT, como `parts_used` del cierre web (Q2)."""
    reservations = Reservation.objects.filter(
        work_order=work_order, status=Reservation.Status.ACTIVE
    ).select_related("part")
    return [{"part": r.part, "quantity": r.pending} for r in reservations if r.pending > 0]


def finish():
    """Fechas derivadas al día con el libro: último movimiento por saldo y última entrada por repuesto."""
    last_by_item = {
        (row["part_id"], row["warehouse_id"]): row["last"]
        for row in StockMovement.objects.values("part_id", "warehouse_id").annotate(last=Max("occurred_at"))
    }
    for item in StockItem.objects.all():
        if (last := last_by_item.get((item.part_id, item.warehouse_id))) is not None:
            StockItem.objects.filter(pk=item.pk).update(last_movement_at=last)
    received = (
        StockMovement.objects.filter(kind=StockMovement.Kind.RECEIPT)
        .values("part_id")
        .annotate(last=Max("occurred_at"))
    )
    for row in received:
        Part.objects.filter(pk=row["part_id"]).update(last_received_at=row["last"])
