from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.utils.translation import gettext_lazy as _

from .models import Membership, User


class PlatformOnlyMixin:
    """Usuarios y membresías son globales: solo el superadmin de plataforma."""

    def has_module_permission(self, request):
        return request.user.is_superuser


@admin.register(User)
class UserAdmin(PlatformOnlyMixin, BaseUserAdmin):
    ordering = ["email"]
    list_display = ["email", "full_name", "is_active", "is_staff"]
    search_fields = ["email", "full_name"]
    list_filter = ["is_active", "is_staff"]
    fieldsets = [
        (None, {"fields": ["email", "password", "full_name"]}),
        (_("Permisos"), {"fields": ["is_active", "is_staff", "is_superuser", "user_permissions"]}),
    ]
    add_fieldsets = [
        (None, {"classes": ["wide"], "fields": ["email", "full_name", "password1", "password2"]})
    ]


@admin.register(Membership)
class MembershipAdmin(PlatformOnlyMixin, admin.ModelAdmin):
    list_display = ["user", "organization", "role", "is_active"]
    list_filter = ["organization", "role"]
