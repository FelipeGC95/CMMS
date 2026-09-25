from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class AttachmentsConfig(AppConfig):
    name = "apps.attachments"
    verbose_name = _("Adjuntos")
    default_auto_field = "django.db.models.BigAutoField"
