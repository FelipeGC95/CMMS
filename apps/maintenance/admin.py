from django.contrib import admin, messages
from django.core.exceptions import ValidationError
from django.shortcuts import redirect
from django.urls import reverse

from apps.core.admin import TenantAdmin, TenantInline

from .models import MaintenancePlan, PlanAssignment, PlanResource, PlanTaskItem, PlanVersion
from .services import assign_plan, publish_version, start_draft


class PlanAssignmentInline(TenantInline):
    model = PlanAssignment
    fields = ["asset", "next_due_on", "meter", "next_due_meter_value", "last_done_on", "is_active"]
    readonly_fields = ["meter", "last_done_on"]
    autocomplete_fields = ["asset"]


class PlanVersionInline(TenantInline):
    model = PlanVersion
    fields = ["number", "status", "frequency_label", "published_at", "change_note"]
    readonly_fields = fields
    show_change_link = True
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False

    @admin.display(description="Frecuencia")
    def frequency_label(self, obj):
        return obj.frequency_label


@admin.register(MaintenancePlan)
class MaintenancePlanAdmin(TenantAdmin):
    """Datos generales del plan. El contenido se edita en borradores y se publica."""

    list_display = ["code", "name", "asset_class", "frequency_label", "current_version", "is_active"]
    list_select_related = ["current_version", "asset_class"]
    list_filter = ["asset_class", "is_active"]
    search_fields = ["code", "name"]
    readonly_fields = ["current_version"]
    inlines = [PlanVersionInline, PlanAssignmentInline]
    actions = ["edit_in_draft"]

    @admin.display(description="Frecuencia")
    def frequency_label(self, obj):
        return obj.frequency_label or "— (sin versión publicada)"

    @admin.action(description="Editar en un borrador (crea o abre la próxima versión)")
    def edit_in_draft(self, request, queryset):
        drafts = [start_draft(plan=plan) for plan in queryset]
        if len(drafts) == 1:
            return redirect(reverse("admin:maintenance_planversion_change", args=[drafts[0].pk]))
        self.message_user(request, f"{len(drafts)} borradores listos para editar.")

    def save_formset(self, request, form, formset, change):
        if formset.model is not PlanAssignment:
            return super().save_formset(request, form, formset, change)
        # Las asignaciones nuevas pasan por el servicio: calcula el primer
        # vencimiento por fecha y por medidor con la versión vigente.
        instances = formset.save(commit=False)
        for obj in formset.deleted_objects:
            obj.delete()
        for instance in instances:
            if not instance._state.adding:
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


class DraftOnlyInline(TenantInline):
    """El checklist y los recursos solo se editan mientras la versión es borrador."""

    def _editable(self, obj):
        return obj is None or obj.is_draft

    def has_add_permission(self, request, obj=None):
        return self._editable(obj) and super().has_add_permission(request, obj)

    def has_change_permission(self, request, obj=None):
        return self._editable(obj) and super().has_change_permission(request, obj)

    def has_delete_permission(self, request, obj=None):
        return self._editable(obj) and super().has_delete_permission(request, obj)


class PlanTaskItemInline(DraftOnlyInline):
    model = PlanTaskItem
    fields = ["order", "text", "item_type", "unit", "min_value", "max_value", "required"]


class PlanResourceInline(DraftOnlyInline):
    model = PlanResource
    fields = ["kind", "description", "quantity", "unit"]


@admin.register(PlanVersion)
class PlanVersionAdmin(TenantAdmin):
    list_display = ["plan", "number", "status", "frequency_label", "published_at"]
    list_filter = ["status"]
    list_select_related = ["plan"]
    search_fields = ["plan__code", "plan__name"]
    fieldsets = [
        (None, {"fields": ["plan", "number", "status", "published_at", "published_by", "change_note"]}),
        (
            "Frecuencia (vence lo que ocurra primero)",
            {"fields": ["interval_days", "meter_kind", "meter_interval", "schedule_mode", "tolerance_pct"]},
        ),
        ("Ejecución", {"fields": ["estimated_minutes", "instructions"]}),
    ]
    inlines = [PlanTaskItemInline, PlanResourceInline]
    actions = ["publish"]

    @admin.display(description="Frecuencia")
    def frequency_label(self, obj):
        return obj.frequency_label

    def get_readonly_fields(self, request, obj=None):
        always = ["plan", "number", "status", "published_at", "published_by"]
        if obj is not None and not obj.is_draft:
            return always + [f for fs in self.fieldsets for f in fs[1]["fields"] if f not in always]
        return always

    def has_add_permission(self, request):
        return False  # los borradores se crean desde el plan (acción «Editar en un borrador»)

    def has_delete_permission(self, request, obj=None):
        return obj is not None and obj.is_draft and super().has_delete_permission(request, obj)

    @admin.action(description="Publicar (pasa a ser la versión vigente)")
    def publish(self, request, queryset):
        for version in queryset:
            try:
                publish_version(version=version, by=request.user, change_note=version.change_note)
            except ValidationError as exc:
                self.message_user(request, f"{version}: {' '.join(exc.messages)}", messages.ERROR)
            else:
                self.message_user(request, f"{version} publicada.", messages.SUCCESS)
