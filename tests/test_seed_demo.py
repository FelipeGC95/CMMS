import pytest
from django.core.management import call_command

from apps.accounts.models import User
from apps.core.tenancy import tenant_context
from apps.organizations.models import Organization
from apps.work.models import WorkOrder
from apps.work.selectors import OPEN_STATUSES

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


def _open_work_orders():
    with tenant_context(Organization.objects.get(slug="alimentos-del-sur")):
        return list(WorkOrder.objects.filter(status__in=OPEN_STATUSES).select_related("assigned_to"))


def test_demo_technicians_have_open_work_orders():
    # "Mis OT" es la vista por defecto del técnico: no debe quedar vacía en la demo.
    call_command("seed_demo", verbosity=0)

    open_orders = _open_work_orders()
    assert open_orders
    assert all(wo.assigned_to is not None for wo in open_orders)
    assert {wo.assigned_to.email for wo in open_orders} == {"tecnico@demo.cl", "tecnico2@demo.cl"}


def test_rerun_assigns_open_work_orders_left_without_technician():
    # La base de Render ya existe: el nuevo arranque corrige las OT sin responsable.
    call_command("seed_demo", verbosity=0)
    with tenant_context(Organization.objects.get(slug="alimentos-del-sur")):
        WorkOrder.objects.filter(status__in=OPEN_STATUSES).update(assigned_to=None)

    call_command("seed_demo", verbosity=0)

    assert all(wo.assigned_to is not None for wo in _open_work_orders())
