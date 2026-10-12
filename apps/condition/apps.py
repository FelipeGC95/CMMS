from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class ConditionConfig(AppConfig):
    name = "apps.condition"
    verbose_name = _("Mantenimiento por condición")
    default_auto_field = "django.db.models.BigAutoField"
