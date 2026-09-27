from django.contrib import admin

from apps.core.admin import TenantAdmin

from .models import Notification


@admin.register(Notification)
class NotificationAdmin(TenantAdmin):
    """Solo consulta: las notificaciones las crean los servicios."""

    list_display = ["created_at", "recipient", "kind", "title", "read_at"]
    list_filter = ["kind"]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
