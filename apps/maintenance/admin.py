from django.contrib import admin, messages
from django.core.exceptions import ValidationError

from apps.core.admin import TenantAdmin, TenantInline

from .models import MaintenancePlan, PlanAssignment, PlanTaskItem
from .services import assign_plan


class PlanTaskItemInline(TenantInline):
    model = PlanTaskItem
    fields = ["order", "text", "item_type", "unit", "min_value", "max_value", "required"]


class PlanAssignmentInline(TenantInline):
    model = PlanAssignment
    fields = ["asset", "next_due_on", "meter", "next_due_meter_value", "last_done_on", "is_active"]
    readonly_fields = ["meter", "last_done_on"]
    autocomplete_fields = ["asset"]


@admin.register(MaintenancePlan)
class MaintenancePlanAdmin(TenantAdmin):
    list_display = ["code", "name", "asset_class", "frequency_label", "is_active"]
    list_filter = ["asset_class", "is_active"]
    search_fields = ["code", "name"]
    fieldsets = [
        (None, {"fields": ["code", "name", "asset_class", "estimated_minutes", "is_active"]}),
        (
            "Frecuencia (vence lo que ocurra primero)",
            {"fields": ["interval_days", "meter_kind", "meter_interval"]},
        ),
    ]
    inlines = [PlanTaskItemInline, PlanAssignmentInline]

    @admin.display(description="Frecuencia")
    def frequency_label(self, obj):
        return obj.frequency_label

    def save_formset(self, request, form, formset, change):
        if formset.model is not PlanAssignment:
            return super().save_formset(request, form, formset, change)
        # Las asignaciones nuevas pasan por el servicio: calcula el primer
        # vencimiento por fecha y por medidor igual que en el resto del sistema.
        instances = formset.save(commit=False)
        for obj in formset.deleted_objects:
            obj.delete()
        for instance in instances:
            if instance.pk and not instance._state.adding:
                instance.save()
                continue
            try:
                assign_plan(
                    plan=form.instance,
                    asset=instance.asset,
                    starts_on=instance.next_due_on,
                    next_due_meter_value=instance.next_due_meter_value,
                )
            except ValidationError as exc:
                messages.error(request, " ".join(exc.messages))
        formset.save_m2m()


@admin.register(PlanAssignment)
class PlanAssignmentAdmin(TenantAdmin):
    list_display = ["plan", "asset", "next_due_on", "next_due_meter_value", "last_done_on", "is_active"]
    list_filter = ["plan", "is_active"]
    readonly_fields = ["meter"]
