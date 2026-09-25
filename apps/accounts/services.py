from allauth.account.models import EmailAddress
from django.db import transaction

from apps.core.middleware import SESSION_KEY

from .models import User


@transaction.atomic
def create_user(*, email, password=None, full_name="", email_verified=False, **extra):
    """Crea el usuario y su correo en allauth.

    `email_verified=True` solo cuando la plataforma ya confía en el correo
    (datos demo, alta hecha por el superadmin); si no, allauth lo verifica al
    primer inicio de sesión.
    """
    user = User.objects.create_user(email, password, full_name=full_name, **extra)
    EmailAddress.objects.create(user=user, email=user.email, primary=True, verified=email_verified)
    return user


def switch_organization(*, request, organization_id):
    """Cambia la organización activa si el usuario es miembro; la recuerda para la próxima sesión."""
    membership = next((m for m in request.memberships if str(m.organization_id) == organization_id), None)
    if membership is None:
        return False
    # Rotar la sesión evita que quede algo de la organización anterior (ORG-07 CA2).
    request.session.cycle_key()
    request.session[SESSION_KEY] = str(membership.organization_id)
    User.objects.filter(pk=request.user.pk).update(last_organization=membership.organization_id)
    return True
