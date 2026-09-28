from django.apps import AppConfig


class FieldConfig(AppConfig):
    """Vista de terreno sin conexión (PWA del técnico, X-06, ADR-011). Sin modelos."""

    name = "apps.field"
    default_auto_field = "django.db.models.BigAutoField"
