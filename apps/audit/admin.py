from django.contrib import admin

from apps.core.admin import TenantAdmin

from .models import AuditEvent


@admin.register(AuditEvent)
class AuditEventAdmin(TenantAdmin):
    """Solo consulta: la auditoría es solo de inserción."""

    list_display = ["created_at", "actor_repr", "action", "target_type", "target_repr", "ip"]
    list_filter = ["action", "target_type"]
    search_fields = ["actor_repr", "target_repr", "target_id"]
    list_select_related = ["target_type"]
    date_hierarchy = "created_at"

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
