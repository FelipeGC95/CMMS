"""Un servidor de correo caído (timeout, puerto bloqueado, rechazo) no da error 500 ni revierte datos."""

from django.core.mail.backends.base import BaseEmailBackend
from django.urls import reverse

from apps.accounts import services
from apps.accounts.models import Invitation, PendingSignup

FAILING = "tests.test_mail_failures.TimeoutBackend"


class TimeoutBackend(BaseEmailBackend):
    """Como Render con el SMTP saliente bloqueado: la conexión vence."""

    def send_messages(self, email_messages):
        raise TimeoutError("timed out")


def test_password_reset_survives_a_mail_timeout(client, tenant_a, settings):
    settings.EMAIL_BACKEND = FAILING
    response = client.post(
        reverse("account_reset_password"), {"email": "technician@empresa-a.cl"}, follow=True
    )
    assert response.status_code == 200
    assert "No pudimos enviar el correo." in response.content.decode()


def test_invitation_is_kept_when_the_mail_fails(tenant_a, settings, django_capture_on_commit_callbacks):
    settings.EMAIL_BACKEND = FAILING
    with django_capture_on_commit_callbacks(execute=True):
        invitation = services.invite_member(
            membership=tenant_a.admin, email="nuevo@empresa-a.cl", role="technician", base_url="https://x"
        )
    assert Invitation.objects.filter(pk=invitation.pk, revoked_at__isnull=True).exists()


def test_signup_request_is_kept_when_the_mail_fails(db, settings):
    settings.EMAIL_BACKEND = FAILING
    pending = services.request_signup(
        email="duena@nueva.cl",
        full_name="Dueña",
        organization_name="Nueva",
        password="una-clave-larga-123",
        confirm_url=lambda token: f"https://x/{token}",
        login_url="https://x/login",
    )
    assert PendingSignup.objects.filter(pk=pending.pk).exists()
