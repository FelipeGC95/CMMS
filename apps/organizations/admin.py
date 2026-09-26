from django import forms
from django.contrib import admin, messages
from django.utils.translation import gettext
from django.utils.translation import gettext_lazy as _

from apps.accounts.services import TeamError, invite_first_admin
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


class OrganizationAdminForm(forms.ModelForm):
    first_admin_email = forms.EmailField(
        label=_("correo del administrador"),
        required=False,
        help_text=_(
            "Al guardar se le envía una invitación como administrador, válida por 7 días (ORG-01). "
            "Si ya tiene una invitación pendiente, se reemplaza."
        ),
    )

    class Meta:
        model = Organization
        fields = [
            "name",
            "slug",
            "plan",
            "status",
            "trial_ends_at",
            "timezone",
            "is_active",
            "require_close_approval",
            "require_failure_details",
        ]


@admin.register(Organization)
class OrganizationAdmin(PlatformOnlyAdmin):
    form = OrganizationAdminForm
    list_display = ["name", "slug", "plan", "status", "trial_ends_at", "is_active"]
    list_filter = ["plan", "status", "is_active"]
    search_fields = ["name", "slug"]
    list_select_related = ["plan"]
    prepopulated_fields = {"slug": ["name"]}

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        email = form.cleaned_data.get("first_admin_email")
        if not email:
            return
        try:
            invitation = invite_first_admin(
                actor=request.user, organization=obj, email=email, base_url=request.build_absolute_uri("/")
            )
        except TeamError as error:
            self.message_user(request, str(error), messages.WARNING)
        else:
            self.message_user(
                request,
                gettext("Invitación de administrador enviada a %(email)s.") % {"email": invitation.email},
                messages.SUCCESS,
            )


@admin.register(Location)
class LocationAdmin(TenantAdmin):
    list_display = ["code", "name", "kind", "parent", "is_active"]
    list_filter = ["kind", "is_active"]
    search_fields = ["code", "name"]
