from django.conf import settings
from django.contrib import admin
from django.core.exceptions import PermissionDenied
from django.template.response import TemplateResponse
from django.urls import path
from django.utils.translation import gettext as _

from . import selectors
from .models import AssistantUsage


@admin.register(AssistantUsage)
class AssistantUsageAdmin(admin.ModelAdmin):
    """Costo estimado del asistente por organización y mes (AI-05 CA2).

    Solo el superadmin de plataforma: muestra datos de todas las organizaciones. Es un informe de
    solo lectura (una sola página, sin fichas ni edición).
    """

    def _is_platform(self, request) -> bool:
        return request.user.is_active and request.user.is_superuser

    def has_module_permission(self, request):
        return self._is_platform(request)

    def has_view_permission(self, request, obj=None):
        return self._is_platform(request)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def get_urls(self):
        name = f"{self.opts.app_label}_{self.opts.model_name}_changelist"
        return [path("", self.admin_site.admin_view(self.changelist_view), name=name)]

    def changelist_view(self, request, extra_context=None):
        if not self._is_platform(request):
            raise PermissionDenied
        rows = selectors.usage_by_organization_month()
        context = {
            **self.admin_site.each_context(request),
            "title": _("Costo del asistente por organización y mes"),
            "opts": self.opts,
            "rows": rows,
            "months": selectors.USAGE_MONTHS,
            "prices": sorted(settings.AI_MODEL_PRICES_USD.items()),
            **(extra_context or {}),
        }
        return TemplateResponse(request, "admin/assistant/usage.html", context)
