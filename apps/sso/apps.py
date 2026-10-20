from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class SsoConfig(AppConfig):
    name = "apps.sso"
    verbose_name = _("Inicio de sesión único (SSO)")
    default_auto_field = "django.db.models.BigAutoField"
