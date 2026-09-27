from allauth.account.adapter import DefaultAccountAdapter
from allauth.mfa.adapter import DefaultMFAAdapter


class AccountAdapter(DefaultAccountAdapter):
    """El registro de allauth (/accounts/signup/) queda cerrado.

    Se entra por invitación (ORG-05), por alta de plataforma o por el autoregistro
    con prueba gratuita de /signup/ (ORG-02), que tiene su propio flujo: guarda la
    solicitud y crea usuario, organización y membresía solo al verificar el correo.
    """

    def is_open_for_signup(self, request):
        return False


class MFAAdapter(DefaultMFAAdapter):
    def get_totp_issuer(self) -> str:
        return "GALBUST CMMS"
