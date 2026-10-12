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


def test_demo_has_an_open_corrective_from_a_failure_report_with_checklist():
    # MVP-0 "Hecho cuando": aviso por QR → OT correctiva → checklist en el celular.
    call_command("seed_demo", verbosity=0)

    with tenant_context(Organization.objects.get(slug="alimentos-del-sur")):
        candidates = [
            wo
            for wo in _open_work_orders()
            if wo.work_type == WorkOrder.Type.CORRECTIVE and hasattr(wo, "request")
        ]
        with_checklist = [wo for wo in candidates if wo.checklist.filter(required=True).exists()]
        assert with_checklist
        wo = with_checklist[0]
        assert wo.assigned_to.email == "tecnico@demo.cl"
        assert wo.checklist.filter(item_type="measurement").exists()


def test_demo_has_inventory_with_history_and_alerts():
    # INV (P7): bodega principal + una por sitio, catálogo, planes enlazados, reservas, consumos
    # históricos (para los indicadores), un repuesto bajo mínimo y uno en negativo.
    from apps.accounts.models import Membership
    from apps.inventory.models import Part, Reservation, Warehouse
    from apps.inventory.selectors import inventory_insights, stock_counters
    from apps.inventory.services import reconcile_stock
    from apps.maintenance.models import PlanResource
    from apps.organizations.models import Location

    call_command("seed_demo", verbosity=0)

    org = Organization.objects.get(slug="alimentos-del-sur")
    admin = Membership.objects.get(organization=org, user__email="jefe@demo.cl")
    with tenant_context(org):
        sites = set(Location.objects.filter(kind=Location.Kind.SITE).values_list("pk", flat=True))
        warehouses = list(Warehouse.objects.all())
        assert sum(w.is_default for w in warehouses) == 1
        assert sites <= {w.location_id for w in warehouses}
        assert Part.objects.count() >= 30
        assert PlanResource.objects.filter(
            part__isnull=False, version__plan__current_version__isnull=False
        ).exists()
        assert Reservation.objects.filter(status=Reservation.Status.ACTIVE).exists()
        counters = stock_counters(membership=admin)
        assert counters["below_min"] >= 1
        assert counters["negative"] >= 1
        insights = inventory_insights(membership=admin)
        assert insights.total_value > 0
        assert insights.consumption > 0
        assert insights.stale_count >= 1
        assert reconcile_stock() == []
