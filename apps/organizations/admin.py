from django.contrib import admin

from apps.core.admin import TenantAdmin

from .models import Location, Organization, Plan


class PlatformOnlyAdmin(admin.ModelAdmin):
    """Organizaciones y planes son de plataforma: solo el superadmin, aunque tenga permisos del modelo."""

    def has_module_permission(self, request):
        return request.user.is_superuser

    def has_view_permission(self, request, obj=None):
        return request.user.is_superuser

    def has_add_permission(self, request):
        return request.user.is_superuser

    def has_change_permission(self, request, obj=None):
        return request.user.is_superuser

    def has_delete_permission(self, request, obj=None):
        return request.user.is_superuser


@admin.register(Plan)
class PlanAdmin(PlatformOnlyAdmin):
    list_display = ["name", "code", "max_assets", "max_users", "max_storage_mb", "is_active"]


@admin.register(Organization)
class OrganizationAdmin(PlatformOnlyAdmin):
    list_display = ["name", "slug", "plan", "status", "trial_ends_at", "is_active"]
    list_filter = ["plan", "status", "is_active"]
    search_fields = ["name", "slug"]
    list_select_related = ["plan"]
    prepopulated_fields = {"slug": ["name"]}


@admin.register(Location)
class LocationAdmin(TenantAdmin):
    list_display = ["code", "name", "kind", "parent", "is_active"]
    list_filter = ["kind", "is_active"]
    search_fields = ["code", "name"]
