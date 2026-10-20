"""Admin de plataforma de la facturación (ORG-13): solo el superadmin.

El valor de la UF se puede cargar a mano (respaldo si la fuente falla). Cuentas, pagos y eventos
del webhook son de solo lectura: los cambia el proveedor; el precio acordado, el cupón y la
exención se editan en la organización.
"""

from django.contrib import admin

from apps.organizations.admin import PlatformOnlyAdmin

from .models import BillingAccount, Payment, ProviderEvent, UfValue


@admin.register(UfValue)
class UfValueAdmin(PlatformOnlyAdmin):
    list_display = ["date", "value", "source"]
    date_hierarchy = "date"


class PlatformReadOnlyAdmin(PlatformOnlyAdmin):
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(BillingAccount)
class BillingAccountAdmin(PlatformReadOnlyAdmin):
    list_display = [
        "organization",
        "plan",
        "period",
        "status",
        "amount_clp",
        "next_charge_at",
        "pending_plan",
    ]
    list_filter = ["status", "period", "plan"]
    search_fields = ["organization__name", "provider_subscription_id", "tax_id", "legal_name"]

    def get_queryset(self, request):
        # all_tenants: vista de plataforma de todas las organizaciones, solo lectura (superadmin).
        return BillingAccount.all_tenants.select_related("organization", "plan", "pending_plan")


@admin.register(Payment)
class PaymentAdmin(PlatformReadOnlyAdmin):
    list_display = ["organization", "paid_at", "amount_clp", "uf_value", "status", "provider_payment_id"]
    list_filter = ["status"]
    search_fields = ["organization__name", "provider_payment_id"]

    def get_queryset(self, request):
        # all_tenants: vista de plataforma de todas las organizaciones, solo lectura (superadmin).
        return Payment.all_tenants.select_related("organization", "plan")


@admin.register(ProviderEvent)
class ProviderEventAdmin(PlatformReadOnlyAdmin):
    list_display = ["created_at", "topic", "resource_id", "organization", "result", "attempts", "detail"]
    list_filter = ["result", "topic"]
    search_fields = ["resource_id", "event_key"]
