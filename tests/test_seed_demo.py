import pytest
from django.core.management import call_command

from apps.accounts.models import User

pytestmark = pytest.mark.django_db

ADMIN_PERMS = {"maintenance.change_planversion", "audit.view_auditevent"}


def _admin():
    # Recarga desde la base: has_perm guarda en caché los permisos de la instancia.
    return User.objects.get(email="jefe@demo.cl")


def test_demo_admin_can_manage_versions_and_read_audit():
    call_command("seed_demo", verbosity=0)

    admin = _admin()
    assert admin.has_perms(ADMIN_PERMS)
    assert not admin.has_perm("audit.change_auditevent")


def test_rerun_restores_admin_permissions_on_existing_data():
    # En Render la demo ya existe y seed_demo corre en cada arranque.
    call_command("seed_demo", verbosity=0)
    _admin().user_permissions.clear()

    call_command("seed_demo", verbosity=0)

    assert _admin().has_perms(ADMIN_PERMS)
