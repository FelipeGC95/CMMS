import logging

from allauth.account.adapter import DefaultAccountAdapter
from allauth.mfa.adapter import DefaultMFAAdapter
from django.contrib import messages
from django.shortcuts import redirect
from django.urls import reverse
from django.utils.http import urlencode
from django.utils.translation import gettext

from apps.core.mail import MAIL_ERRORS

logger = logging.getLogger(__name__)


class AccountAdapter(DefaultAccountAdapter):
    """El registro de allauth (/accounts/signup/) queda cerrado.

    Se entra por invitación (ORG-05), por alta de plataforma o por el autoregistro
    con prueba gratuita de /signup/ (ORG-02), que tiene su propio flujo: guarda la
    solicitud y crea usuario, organización y membresía solo al verificar el correo.
    """

    def is_open_for_signup(self, request):
        return False

    def send_mail(self, template_prefix, email, context):
        """Recuperar clave, verificar correo…: si el servidor de correo falla se registra y se
        avisa en pantalla, en vez de un error 500."""
        try:
            super().send_mail(template_prefix, email, context)
        except MAIL_ERRORS:
            logger.exception("No se pudo enviar el correo %s", template_prefix)
            if self.request is not None:
                messages.error(
                    self.request,
                    gettext("No pudimos enviar el correo. Inténtalo de nuevo en unos minutos."),
                    fail_silently=True,
                )

    def pre_login(self, request, user, **kwargs):
        """«Exigir SSO» (ORG-11 CA7): con contraseña (o enlace de correo) no entra quien
        debe usar el SSO de la organización dueña de su dominio; se le manda al SSO.
        Los administradores de esa organización sí (acceso de emergencia con MFA)."""
        from apps.sso.selectors import password_login_blocked

        organization = password_login_blocked(user)
        if organization is not None:
            messages.info(
                request,
                gettext("%(organization)s exige entrar con inicio de sesión único (SSO).")
                % {"organization": organization.name},
            )
            return redirect(f"{reverse('sso_login')}?{urlencode({'email': user.email})}")
        return super().pre_login(request, user, **kwargs)


class MFAAdapter(DefaultMFAAdapter):
    def get_totp_issuer(self) -> str:
        return "GALBUST CMMS"
