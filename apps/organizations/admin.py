from django.contrib import admin

from apps.core.admin import TenantAdmin

from .models import Location, Organization


@admin.register(Organization)
class OrganizationAdmin(admin.ModelAdmin):
    list_display = ["name", "slug", "timezone", "is_active"]
    prepopulated_fields = {"slug": ["name"]}

    def has_module_permission(self, request):
        return request.user.is_superuser


@admin.register(Location)
class LocationAdmin(TenantAdmin):
    list_display = ["code", "name", "kind", "parent", "is_active"]
    list_filter = ["kind", "is_active"]
    search_fields = ["code", "name"]
