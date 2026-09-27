from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class NotificationsConfig(AppConfig):
    name = "apps.notifications"
    verbose_name = _("Notificaciones")
    default_auto_field = "django.db.models.BigAutoField"
