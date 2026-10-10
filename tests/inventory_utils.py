"""Ayudas de los tests de inventario (INV)."""

from apps.accounts.models import Membership, User
from apps.work.models import WorkOrder
from apps.work.services import create_work_order
from tests.conftest import PASSWORD


def add_member(tenant, role, *, suffix=""):
    user = User.objects.create_user(f"{role}{suffix}@{tenant.org.slug}.cl", PASSWORD, full_name=role)
    return Membership.objects.create(user=user, organization=tenant.org, role=role)


def make_work_order(tenant, **kwargs):
    kwargs.setdefault("work_type", WorkOrder.Type.CORRECTIVE)
    kwargs.setdefault("title", "Cambio de rodamiento")
    if "location" not in kwargs:
        kwargs.setdefault("asset", tenant.asset)
    return create_work_order(**kwargs)


def set_status(work_order, status):
    WorkOrder.objects.filter(pk=work_order.pk).update(status=status)
    work_order.refresh_from_db()
    return work_order
