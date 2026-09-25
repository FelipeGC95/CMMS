from allauth.account.adapter import DefaultAccountAdapter


class AccountAdapter(DefaultAccountAdapter):
    """Sin autoregistro: se entra por invitación o alta de plataforma.

    El autoregistro con prueba gratuita (ORG-02) llega en MVP-1.
    """

    def is_open_for_signup(self, request):
        return False
