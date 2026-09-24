from django.contrib import admin

from apps.core.admin import TenantAdmin, TenantInline

from .models import MaintenancePlan, PlanAssignment, PlanTaskItem


class PlanTaskItemInline(TenantInline):
    model = PlanTaskItem
    fields = ["order", "text", "item_type", "unit", "min_value", "max_value", "required"]


class PlanAssignmentInline(TenantInline):
    model = PlanAssignment
    fields = ["asset", "next_due_on", "last_done_on", "is_active"]
    autocomplete_fields = ["asset"]


@admin.register(MaintenancePlan)
class MaintenancePlanAdmin(TenantAdmin):
    list_display = ["code", "name", "asset_class", "interval_days", "is_active"]
    list_filter = ["asset_class", "is_active"]
    search_fields = ["code", "name"]
    inlines = [PlanTaskItemInline, PlanAssignmentInline]


@admin.register(PlanAssignment)
class PlanAssignmentAdmin(TenantAdmin):
    list_display = ["plan", "asset", "next_due_on", "last_done_on", "is_active"]
    list_filter = ["plan", "is_active"]
