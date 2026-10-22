"""Avisos de configuración de producción (apps/core/checks.py)."""

from apps.core.checks import check_email_delivery

CONSOLE = "django.core.mail.backends.console.EmailBackend"
SMTP = "django.core.mail.backends.smtp.EmailBackend"


def _ids():
    return [warning.id for warning in check_email_delivery(None)]


def test_production_without_smtp_or_contact_warns(settings):
    settings.DEBUG = False
    settings.EMAIL_BACKEND = CONSOLE
    settings.BILLING_CONTACT_EMAIL = ""
    assert _ids() == ["core.W001", "core.W002"]


def test_configured_production_is_quiet(settings):
    settings.DEBUG = False
    settings.EMAIL_BACKEND = SMTP
    settings.BILLING_CONTACT_EMAIL = "contacto@galbust.com"
    assert _ids() == []


def test_queue_checks_the_real_delivery_backend(settings):
    settings.DEBUG = False
    settings.EMAIL_BACKEND = "apps.core.mail.QueuedEmailBackend"
    settings.EMAIL_DELIVERY_BACKEND = CONSOLE
    settings.BILLING_CONTACT_EMAIL = "contacto@galbust.com"
    assert _ids() == ["core.W001"]


def test_local_development_is_quiet(settings):
    settings.DEBUG = True
    settings.EMAIL_BACKEND = CONSOLE
    settings.BILLING_CONTACT_EMAIL = ""
    assert _ids() == []
