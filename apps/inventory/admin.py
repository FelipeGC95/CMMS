from django.contrib import admin

from apps.core.admin import TenantAdmin

from .models import Part, Reservation, StockItem, StockMovement, Warehouse


class ReadOnlyTenantAdmin(TenantAdmin):
    """Solo consulta: el inventario cambia por servicios (saldo = libro, auditoría, INV-04/14)."""

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Part)
class PartAdmin(ReadOnlyTenantAdmin):
    list_display = ["code", "name", "unit", "category", "avg_cost", "is_active"]
    list_filter = ["is_active", "category"]
    search_fields = ["code", "name", "manufacturer_ref"]


@admin.register(Warehouse)
class WarehouseAdmin(ReadOnlyTenantAdmin):
    list_display = ["code", "name", "location", "is_default", "is_active"]
    list_select_related = ["location"]


@admin.register(StockItem)
class StockItemAdmin(ReadOnlyTenantAdmin):
    list_display = ["part", "warehouse", "on_hand", "reserved", "min_quantity", "last_movement_at"]
    list_select_related = ["part", "warehouse"]
    search_fields = ["part__code", "part__name"]


@admin.register(StockMovement)
class StockMovementAdmin(ReadOnlyTenantAdmin):
    """El libro es inmutable: ni el admin lo edita ni lo borra."""

    list_display = ["occurred_at", "kind", "part", "warehouse", "quantity", "unit_cost", "on_hand_after"]
    list_filter = ["kind", "reason"]
    list_select_related = ["part", "warehouse"]
    search_fields = ["part__code", "part__name", "reference"]
    date_hierarchy = "occurred_at"


@admin.register(Reservation)
class ReservationAdmin(ReadOnlyTenantAdmin):
    list_display = ["work_order", "part", "warehouse", "quantity", "consumed_quantity", "status"]
    list_filter = ["status", "source"]
    list_select_related = ["work_order", "part", "warehouse"]
