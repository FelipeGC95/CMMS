from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class AuditConfig(AppConfig):
    name = "apps.audit"
    verbose_name = _("Auditoría")
    default_auto_field = "django.db.models.BigAutoField"
