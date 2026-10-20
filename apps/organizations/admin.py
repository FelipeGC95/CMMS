from django import forms
from django.contrib import admin, messages
from django.utils import timezone
from django.utils.translation import gettext
from django.utils.translation import gettext_lazy as _

from apps.accounts.services import TeamError, invite_first_admin
from apps.core.admin import TenantAdmin

from .models import Coupon, Location, Organization, Plan


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
    list_display = [
        "name",
        "code",
        "max_assets",
        "max_users",
        "max_storage_mb",
        "max_assistant_conversations",
        "monthly_price_uf",
        "is_self_service",
        "is_active",
    ]
    fieldsets = [
        (
            None,
            {
                "fields": [
                    "code",
                    "name",
                    "is_active",
                    "max_assets",
                    "max_users",
                    "max_storage_mb",
                    "max_assistant_conversations",
                    "features",
                ]
            },
        ),
        (
            _("Precio (ORG-13)"),
            {
                "fields": [
                    "monthly_price_uf",
                    "price_included_assets",
                    "price_block_assets",
                    "price_block_uf",
                    "annual_months_charged",
                    "is_self_service",
                ],
                "description": _(
                    "En UF por mes. Los cobros en curso se actualizan al nuevo precio en la tarea diaria. "
                    "Empresa: precio de referencia («desde») y contratable solo con precio acordado."
                ),
            },
        ),
    ]


@admin.register(Coupon)
class CouponAdmin(PlatformOnlyAdmin):
    list_display = ["name", "code", "percent_off", "duration_months", "is_active"]
    prepopulated_fields = {"code": ["name"]}


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
            "billing_exempt",
            "paid_through",
            "past_due_since",
            "custom_monthly_uf",
            "coupon",
        ]


@admin.register(Organization)
class OrganizationAdmin(PlatformOnlyAdmin):
    form = OrganizationAdminForm
    list_display = ["name", "slug", "plan", "status", "billing_access", "trial_ends_at", "is_active"]
    list_filter = ["plan", "status", "billing_exempt", "is_active"]
    search_fields = ["name", "slug"]
    list_select_related = ["plan"]
    prepopulated_fields = {"slug": ["name"]}

    @admin.display(description=_("acceso por facturación"))
    def billing_access(self, obj):
        from apps.billing.access import billing_status

        return billing_status(obj).access.value

    def save_model(self, request, obj, form, change):
        # Cupón (ORG-13): su duración corre desde que se aplica o se cambia.
        if "coupon" in form.changed_data:
            obj.coupon_applied_at = timezone.now() if obj.coupon_id else None
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
