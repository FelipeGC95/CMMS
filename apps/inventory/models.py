"""Inventario y repuestos (INV, F2; ADR-028).

El libro de movimientos (`StockMovement`) es inmutable y manda; el saldo por
repuesto y bodega (`StockItem`) es una caché transaccional que solo cambian
los servicios de `apps.inventory.services`, siempre junto con un movimiento.
"""

from decimal import Decimal

from django.conf import settings
from django.db import models
from django.db.models import F, Q
from django.db.models.functions import Upper
from django.utils.translation import gettext_lazy as _

from apps.core.models import PlatformQuerySetMixin, TenantManager, TenantModel

QUANTITY = {"max_digits": 14, "decimal_places": 3}
MONEY = {"max_digits": 14, "decimal_places": 2}
UNIT_COST = {"max_digits": 18, "decimal_places": 4}


class Part(TenantModel):
    """Repuesto o material del catálogo (INV-01). No se borra: se desactiva."""

    code = models.CharField(_("código"), max_length=40)
    name = models.CharField(_("nombre"), max_length=200)
    description = models.TextField(_("descripción"), blank=True)
    unit = models.CharField(_("unidad"), max_length=20, default="un")
    category = models.CharField(_("categoría"), max_length=80, blank=True)
    manufacturer = models.CharField(_("fabricante"), max_length=120, blank=True)
    manufacturer_ref = models.CharField(_("n° de parte"), max_length=80, blank=True)
    supplier = models.CharField(_("proveedor"), max_length=150, blank=True)
    notes = models.TextField(_("notas"), blank=True)
    # Costo promedio ponderado de la organización (INV-08): lo cambian solo los movimientos.
    avg_cost = models.DecimalField(_("costo promedio"), default=Decimal(0), **UNIT_COST)
    last_cost = models.DecimalField(_("último costo"), null=True, blank=True, **UNIT_COST)
    last_received_at = models.DateTimeField(_("última entrada"), null=True, blank=True)
    is_active = models.BooleanField(_("activo"), default=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("creado por"),
    )

    class Meta:
        verbose_name = _("repuesto")
        verbose_name_plural = _("repuestos")
        ordering = ["code"]
        constraints = [
            models.UniqueConstraint(F("organization"), Upper("code"), name="uniq_part_code_ci"),
            models.CheckConstraint(condition=Q(avg_cost__gte=0), name="part_avg_cost_gte_0"),
        ]
        indexes = [
            models.Index(fields=["organization", "is_active", "name"], name="part_org_active_name_idx"),
            models.Index(fields=["organization", "category"], name="part_org_category_idx"),
        ]

    def __str__(self):
        return f"{self.code} · {self.name}"


class Warehouse(TenantModel):
    """Bodega (INV-03). Exactamente una por defecto; se crea sola al primer uso."""

    code = models.CharField(_("código"), max_length=20)
    name = models.CharField(_("nombre"), max_length=120)
    location = models.ForeignKey(
        "organizations.Location",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("ubicación"),
    )
    is_default = models.BooleanField(_("por defecto"), default=False)
    is_active = models.BooleanField(_("activa"), default=True)
    notes = models.TextField(_("notas"), blank=True)

    class Meta:
        verbose_name = _("bodega")
        verbose_name_plural = _("bodegas")
        ordering = ["-is_default", "name"]
        constraints = [
            models.UniqueConstraint(F("organization"), Upper("code"), name="uniq_warehouse_code_ci"),
            models.UniqueConstraint(
                fields=["organization"], condition=Q(is_default=True), name="uniq_default_warehouse"
            ),
            models.CheckConstraint(
                condition=~Q(is_default=True, is_active=False), name="warehouse_default_is_active"
            ),
        ]
        indexes = [models.Index(fields=["organization", "location"], name="warehouse_org_location_idx")]

    def __str__(self):
        return f"{self.code} · {self.name}"


class StockItem(TenantModel):
    """Saldo de un repuesto en una bodega (INV-04). Solo lo cambian los movimientos y reservas."""

    part = models.ForeignKey(
        Part, on_delete=models.PROTECT, related_name="stock_items", verbose_name=_("repuesto")
    )
    warehouse = models.ForeignKey(
        Warehouse, on_delete=models.PROTECT, related_name="stock_items", verbose_name=_("bodega")
    )
    # Puede ser negativo (Q1): un consumo nunca se rechaza por falta de stock.
    on_hand = models.DecimalField(_("existencia"), default=Decimal(0), **QUANTITY)
    reserved = models.DecimalField(_("reservado"), default=Decimal(0), **QUANTITY)
    min_quantity = models.DecimalField(_("stock mínimo"), default=Decimal(0), **QUANTITY)
    target_quantity = models.DecimalField(_("stock objetivo"), null=True, blank=True, **QUANTITY)
    bin_location = models.CharField(_("ubicación en bodega"), max_length=40, blank=True)
    last_movement_at = models.DateTimeField(_("último movimiento"), null=True, blank=True)

    class Meta:
        verbose_name = _("stock")
        verbose_name_plural = _("stock")
        constraints = [
            models.UniqueConstraint(fields=["organization", "part", "warehouse"], name="uniq_stock_item"),
            models.CheckConstraint(condition=Q(reserved__gte=0), name="stock_reserved_gte_0"),
            models.CheckConstraint(condition=Q(min_quantity__gte=0), name="stock_min_gte_0"),
            models.CheckConstraint(
                condition=Q(target_quantity__isnull=True) | Q(target_quantity__gte=F("min_quantity")),
                name="stock_target_gte_min",
            ),
        ]
        indexes = [
            models.Index(fields=["organization", "warehouse"], name="stock_org_warehouse_idx"),
            models.Index(fields=["organization", "part"], name="stock_org_part_idx"),
        ]

    def __str__(self):
        return f"{self.part_id} @ {self.warehouse_id}"

    @property
    def available(self) -> Decimal:
        return self.on_hand - self.reserved

    @property
    def is_below_min(self) -> bool:
        """Bajo mínimo (INV-12): disponible < mínimo, con un mínimo definido."""
        return self.min_quantity > 0 and self.available < self.min_quantity


class ImmutableMovementError(RuntimeError):
    """Se intentó modificar o borrar un movimiento de stock (el libro es inmutable)."""


class StockMovementQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise ImmutableMovementError("Los movimientos de stock no se modifican.")

    def delete(self):
        raise ImmutableMovementError("Los movimientos de stock no se borran.")

    update.queryset_only = True
    delete.queryset_only = True


class PlatformStockMovementQuerySet(PlatformQuerySetMixin, StockMovementQuerySet):
    """`all_tenants` de movimientos: sin RLS (ADR-026) e igual de inmutable."""

    update = StockMovementQuerySet.update
    delete = StockMovementQuerySet.delete


class StockMovementManager(TenantManager.from_queryset(StockMovementQuerySet)):
    pass


class StockMovement(TenantModel):
    """Libro inmutable de stock (INV-14). Un error se corrige con un movimiento contrario.

    La purga de una organización (ADR-027) usa `_raw_delete` sobre `_base_manager`,
    que no pasa por este queryset.
    """

    class Kind(models.TextChoices):
        RECEIPT = "receipt", _("Entrada")
        ISSUE = "issue", _("Salida a OT")
        RETURN = "return", _("Devolución desde OT")
        ADJUSTMENT = "adjustment", _("Ajuste")
        TRANSFER_OUT = "transfer_out", _("Transferencia (salida)")
        TRANSFER_IN = "transfer_in", _("Transferencia (entrada)")

    class Reason(models.TextChoices):
        NONE = "", _("Sin motivo")
        COUNT = "count", _("Conteo")
        IMPORT = "import", _("Importación")
        INITIAL = "initial", _("Stock inicial")
        RECONCILE = "reconcile", _("Reconciliación")

    part = models.ForeignKey(
        Part, on_delete=models.PROTECT, related_name="movements", verbose_name=_("repuesto")
    )
    warehouse = models.ForeignKey(
        Warehouse, on_delete=models.PROTECT, related_name="movements", verbose_name=_("bodega")
    )
    kind = models.CharField(_("tipo"), max_length=20, choices=Kind.choices)
    reason = models.CharField(_("motivo"), max_length=20, choices=Reason.choices, blank=True, default="")
    # Con signo: positivo entra a la bodega, negativo sale.
    quantity = models.DecimalField(_("cantidad"), **QUANTITY)
    unit_cost = models.DecimalField(_("costo unitario"), **UNIT_COST)
    total_cost = models.DecimalField(_("costo total"), **MONEY)
    avg_cost_after = models.DecimalField(_("costo promedio resultante"), **UNIT_COST)
    on_hand_after = models.DecimalField(_("saldo resultante"), **QUANTITY)
    work_order = models.ForeignKey(
        "work.WorkOrder",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="stock_movements",
        verbose_name=_("OT"),
    )
    # Devolución → la salida que revierte.
    reverses = models.ForeignKey(
        "self",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="reversals",
        verbose_name=_("revierte a"),
    )
    transfer_group = models.UUIDField(_("transferencia"), null=True, blank=True)
    reference = models.CharField(_("documento"), max_length=60, blank=True)
    supplier = models.CharField(_("proveedor"), max_length=150, blank=True)
    note = models.CharField(_("nota"), max_length=500, blank=True)
    occurred_at = models.DateTimeField(_("fecha"))
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("registrado por"),
    )
    client_uuid = models.UUIDField(null=True, blank=True, verbose_name=_("UUID del cliente"))

    objects = StockMovementManager()
    all_tenants = models.Manager.from_queryset(PlatformStockMovementQuerySet)()  # noqa: DJ012

    class Meta:
        verbose_name = _("movimiento de stock")
        verbose_name_plural = _("movimientos de stock")
        ordering = ["-occurred_at", "-created_at"]
        constraints = [
            models.CheckConstraint(condition=~Q(quantity=0), name="movement_quantity_not_zero"),
            models.CheckConstraint(
                condition=Q(kind__in=["receipt", "return", "transfer_in"], quantity__gt=0)
                | Q(kind__in=["issue", "transfer_out"], quantity__lt=0)
                | Q(kind="adjustment"),
                name="movement_quantity_sign",
            ),
            models.CheckConstraint(
                condition=~Q(kind="issue") | Q(work_order__isnull=False), name="movement_issue_has_work_order"
            ),
            models.UniqueConstraint(fields=["organization", "client_uuid"], name="uniq_movement_client_uuid"),
        ]
        indexes = [
            models.Index(fields=["organization", "part", "-occurred_at"], name="movement_org_part_idx"),
            models.Index(fields=["organization", "warehouse", "-occurred_at"], name="movement_org_wh_idx"),
            models.Index(
                fields=["organization", "work_order"],
                condition=Q(work_order__isnull=False),
                name="movement_org_wo_idx",
            ),
            models.Index(fields=["organization", "kind", "occurred_at"], name="movement_org_kind_idx"),
        ]

    def __str__(self):
        return f"{self.get_kind_display()} {self.quantity} · {self.part_id}"

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ImmutableMovementError("Los movimientos de stock no se modifican.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ImmutableMovementError("Los movimientos de stock no se borran.")


class Reservation(TenantModel):
    """Repuesto reservado para una OT (INV-10). Informa, no bloquea.

    Invariante: `StockItem.reserved` = Σ pendientes de las reservas activas de ese repuesto y bodega.
    """

    class Status(models.TextChoices):
        ACTIVE = "active", _("Activa")
        FULFILLED = "fulfilled", _("Cubierta")
        RELEASED = "released", _("Liberada")

    class Source(models.TextChoices):
        PLAN = "plan", _("Plan preventivo")
        MANUAL = "manual", _("Manual")

    class ReleaseReason(models.TextChoices):
        NONE = "", _("Sin motivo")
        CLOSED = "closed", _("OT cerrada")
        CANCELLED = "cancelled", _("OT cancelada")
        MANUAL = "manual", _("Liberada a mano")

    work_order = models.ForeignKey(
        "work.WorkOrder", on_delete=models.PROTECT, related_name="reservations", verbose_name=_("OT")
    )
    part = models.ForeignKey(
        Part, on_delete=models.PROTECT, related_name="reservations", verbose_name=_("repuesto")
    )
    warehouse = models.ForeignKey(
        Warehouse, on_delete=models.PROTECT, related_name="reservations", verbose_name=_("bodega")
    )
    quantity = models.DecimalField(_("cantidad"), **QUANTITY)
    consumed_quantity = models.DecimalField(_("consumido"), default=Decimal(0), **QUANTITY)
    status = models.CharField(_("estado"), max_length=10, choices=Status.choices, default=Status.ACTIVE)
    source = models.CharField(_("origen"), max_length=10, choices=Source.choices)
    plan_resource = models.ForeignKey(
        "maintenance.PlanResource",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("recurso del plan"),
    )
    release_reason = models.CharField(
        _("motivo de liberación"), max_length=10, choices=ReleaseReason.choices, blank=True, default=""
    )
    released_at = models.DateTimeField(_("liberada el"), null=True, blank=True)
    # Nulo = la creó el sistema (generación de preventivas).
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("creada por"),
    )

    class Meta:
        verbose_name = _("reserva de repuesto")
        verbose_name_plural = _("reservas de repuestos")
        ordering = ["created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "work_order", "part"],
                condition=Q(status="active"),
                name="uniq_active_reservation",
            ),
            models.CheckConstraint(condition=Q(quantity__gt=0), name="reservation_quantity_gt_0"),
            models.CheckConstraint(condition=Q(consumed_quantity__gte=0), name="reservation_consumed_gte_0"),
        ]
        indexes = [
            models.Index(fields=["organization", "work_order"], name="reservation_org_wo_idx"),
            models.Index(fields=["organization", "part", "status"], name="reservation_org_part_idx"),
        ]

    def __str__(self):
        return f"{self.part_id} × {self.quantity}"

    @property
    def pending(self) -> Decimal:
        """Lo que falta consumir: max(cantidad − consumido, 0)."""
        return max(self.quantity - self.consumed_quantity, Decimal(0))
