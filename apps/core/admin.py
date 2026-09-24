from django.contrib import admin


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


class TenantInline(admin.TabularInline):
    extra = 0
