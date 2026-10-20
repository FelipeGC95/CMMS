from allauth.account.adapter import DefaultAccountAdapter
from allauth.mfa.adapter import DefaultMFAAdapter
from django.contrib import messages
from django.shortcuts import redirect
from django.urls import reverse
from django.utils.http import urlencode
from django.utils.translation import gettext


class AccountAdapter(DefaultAccountAdapter):
    """El registro de allauth (/accounts/signup/) queda cerrado.

    Se entra por invitación (ORG-05), por alta de plataforma o por el autoregistro
    con prueba gratuita de /signup/ (ORG-02), que tiene su propio flujo: guarda la
    solicitud y crea usuario, organización y membresía solo al verificar el correo.
    """

    def is_open_for_signup(self, request):
        return False

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
