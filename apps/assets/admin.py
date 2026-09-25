from django.contrib import admin

from apps.core.admin import TenantAdmin, TenantInline

from .models import Asset, AssetClass, Meter


@admin.register(AssetClass)
class AssetClassAdmin(TenantAdmin):
    list_display = ["code", "name", "is_active"]
    search_fields = ["code", "name"]


class MeterInline(TenantInline):
    model = Meter
    fields = ["kind", "name", "unit", "is_cumulative", "current_value", "current_read_at", "is_active"]
    # Las lecturas se registran desde la ficha del activo (validan y disparan preventivos).
    readonly_fields = ["current_value", "current_read_at"]


@admin.register(Asset)
class AssetAdmin(TenantAdmin):
    list_display = ["code", "name", "asset_class", "location", "status", "criticality"]
    list_filter = ["status", "criticality", "asset_class"]
    search_fields = ["code", "name", "serial_number"]
    # El estado se cambia con servicios (registra períodos de detención).
    readonly_fields = ["status"]
    inlines = [MeterInline]
