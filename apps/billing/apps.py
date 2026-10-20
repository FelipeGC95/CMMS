from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class BillingConfig(AppConfig):
    name = "apps.billing"
    verbose_name = _("Facturación")
    default_auto_field = "django.db.models.BigAutoField"
