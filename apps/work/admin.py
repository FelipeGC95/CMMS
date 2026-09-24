from django.contrib import admin

from apps.core.admin import TenantAdmin

from .models import FailureCode, FailureEvent, WorkOrder


@admin.register(WorkOrder)
class WorkOrderAdmin(TenantAdmin):
    """Solo consulta: las OT se crean y cambian por servicios (reglas y auditoría)."""

    list_display = ["code", "title", "work_type", "status", "priority", "asset", "due_on"]
    list_filter = ["work_type", "status", "priority"]
    search_fields = ["title", "asset__code", "asset__name"]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(FailureCode)
class FailureCodeAdmin(TenantAdmin):
    list_display = ["kind", "code", "name", "is_active"]
    list_filter = ["kind", "is_active"]


@admin.register(FailureEvent)
class FailureEventAdmin(TenantAdmin):
    list_display = ["asset", "impact", "failure_mode", "failure_cause", "started_at", "ended_at"]
    list_filter = ["impact"]

    def has_add_permission(self, request):
        return False
