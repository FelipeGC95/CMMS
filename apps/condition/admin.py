from django.contrib import admin

from apps.core.admin import TenantAdmin

from .models import ConditionAlert, ConditionRule, ConditionState


class ReadOnlyTenantAdmin(TenantAdmin):
    """Solo consulta: reglas y alertas cambian por servicios (validación, histéresis, auditoría)."""

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(ConditionRule)
class ConditionRuleAdmin(ReadOnlyTenantAdmin):
    list_display = ["__str__", "operator", "consecutive", "action", "is_active"]
    list_filter = ["is_active", "operator", "action"]


@admin.register(ConditionState)
class ConditionStateAdmin(ReadOnlyTenantAdmin):
    list_display = ["rule", "meter", "status", "consecutive_count", "last_value", "last_evaluated_at"]
    list_filter = ["status"]


@admin.register(ConditionAlert)
class ConditionAlertAdmin(ReadOnlyTenantAdmin):
    list_display = ["meter", "status", "value", "last_value", "opened_at", "work_order"]
    list_filter = ["status"]
