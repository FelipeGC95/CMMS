"""Verificaciones de configuración (`manage.py check`, también al migrar en cada arranque).

Avisan en los logs cuando producción queda sin algo que el cliente espera: hoy, el
correo. Sin proveedor SMTP los correos (verificación, invitaciones, claves,
notificaciones) solo se escriben en la consola y nadie los recibe.
"""

from django.conf import settings
from django.core.checks import Warning, register

LOCAL_EMAIL_BACKENDS = (
    "django.core.mail.backends.console.EmailBackend",
    "django.core.mail.backends.locmem.EmailBackend",
    "django.core.mail.backends.dummy.EmailBackend",
    "django.core.mail.backends.filebased.EmailBackend",
)


@register()
def check_email_delivery(app_configs, **kwargs):
    if settings.DEBUG:
        return []
    backend = getattr(settings, "EMAIL_DELIVERY_BACKEND", settings.EMAIL_BACKEND)
    warnings = []
    if backend in LOCAL_EMAIL_BACKENDS:
        warnings.append(
            Warning(
                "Correo sin proveedor: los correos se escriben en la consola y no se envían.",
                hint="Definir EMAIL_HOST, EMAIL_HOST_USER, EMAIL_HOST_PASSWORD y DEFAULT_FROM_EMAIL "
                "(docs/07_operacion.md).",
                id="core.W001",
            )
        )
    if not settings.BILLING_CONTACT_EMAIL:
        warnings.append(
            Warning(
                "BILLING_CONTACT_EMAIL vacío: /settings/billing/ no muestra cómo cotizar ni contratar.",
                hint="Definir un correo que alguien lea (docs/07_operacion.md).",
                id="core.W002",
            )
        )
    return warnings
