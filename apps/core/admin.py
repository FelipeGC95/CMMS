from django.contrib import admin

from apps.audit import services as audit

from .models import TenantModel
from .tenancy import get_current_organization


class TenantAdmin(admin.ModelAdmin):
    """Admin de modelos de negocio: solo opera dentro de la organización activa.

    El filtrado lo hace el manager por defecto de TenantModel, que usa el
    contexto fijado por TenantMiddleware.
    """

    def _has_tenant(self, request):
        return getattr(request, "organization", None) is not None

    def has_module_permission(self, request):
        return self._has_tenant(request) and super().has_module_permission(request)

    def has_view_permission(self, request, obj=None):
        return self._has_tenant(request) and super().has_view_permission(request, obj)

    def has_add_permission(self, request):
        return self._has_tenant(request) and super().has_add_permission(request)

    def has_change_permission(self, request, obj=None):
        return self._has_tenant(request) and super().has_change_permission(request, obj)

    def has_delete_permission(self, request, obj=None):
        return self._has_tenant(request) and super().has_delete_permission(request, obj)

    # Auditoría (X-01) de altas, ediciones y eliminaciones hechas en el admin.
    # Se engancha aquí y no en post_save de LogEntry porque las eliminaciones
    # masivas del admin usan bulk_create y no emiten señales.

    def get_object(self, request, object_id, from_field=None):
        obj = super().get_object(request, object_id, from_field)
        if obj is not None:
            request._audit_before = {obj.pk: audit.snapshot(obj)}
        return obj

    def log_addition(self, request, obj, message):
        entry = super().log_addition(request, obj, message)
        self._audit(request, obj, "admin_create", {"fields": audit.snapshot(obj), "message": message})
        return entry

    def log_change(self, request, obj, message):
        entry = super().log_change(request, obj, message)
        before = getattr(request, "_audit_before", {}).get(obj.pk)
        changes = {
            "fields": audit.diff(before, audit.snapshot(obj)) if before is not None else {},
            "message": message,
        }
        self._audit(request, obj, "admin_update", changes)
        return entry

    def log_deletions(self, request, queryset):
        entries = super().log_deletions(request, queryset)
        for obj in queryset:
            self._audit(request, obj, "admin_delete", {"fields": audit.snapshot(obj)})
        return entries

    def _audit(self, request, obj, action, changes):
        # Objetos sin tenant o sin contexto son auditoría de plataforma (etapa 2).
        if not isinstance(obj, TenantModel) or get_current_organization() is None:
            return
        audit.record(action=action, target=obj, actor=request.user, changes=changes)


class TenantInline(admin.TabularInline):
    extra = 0
