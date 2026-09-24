from django.contrib import admin

from apps.core.admin import TenantAdmin

from .models import Asset, AssetClass


@admin.register(AssetClass)
class AssetClassAdmin(TenantAdmin):
    list_display = ["code", "name", "is_active"]
    search_fields = ["code", "name"]


@admin.register(Asset)
class AssetAdmin(TenantAdmin):
    list_display = ["code", "name", "asset_class", "location", "status", "criticality"]
    list_filter = ["status", "criticality", "asset_class"]
    search_fields = ["code", "name", "serial_number"]
    # El estado se cambia con servicios (registra períodos de detención).
    readonly_fields = ["status"]
