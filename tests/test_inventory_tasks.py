"""Reconciliación semanal de inventario (INV-14 CA4): tarea periódica sin --fix que avisa con un log
de advertencia. Como en tests/test_tasks.py, se prueban las funciones sin tocar la cola."""

import logging

from django.utils import timezone
from procrastinate.contrib.django import app

from apps.core.tenancy import tenant_context
from apps.inventory import services
from apps.inventory import tasks as inventory_tasks
from apps.inventory.models import StockItem
from apps.inventory.services import reconcile_stock


def test_weekly_reconciliation_is_registered():
    periodic = {p.task.name: p for p in app.periodic_registry.periodic_tasks.values()}
    assert periodic["inventory.enqueue_reconciliation"].cron == "0 9 * * 1"


def test_weekly_task_enqueues_one_job_per_active_organization(tenant_a, tenant_b, monkeypatch):
    tenant_b.org.is_active = False
    tenant_b.org.save(update_fields=["is_active"])
    deferred = []

    class Deferrer:
        def __init__(self, lock):
            self.lock = lock

        def defer(self, **kwargs):
            deferred.append((self.lock, kwargs))

    monkeypatch.setattr(
        inventory_tasks.reconcile_organization_stock,
        "configure",
        lambda queueing_lock: Deferrer(queueing_lock),
    )
    assert inventory_tasks.enqueue_reconciliation(timestamp=0) == 1
    org_id = str(tenant_a.org.pk)
    assert deferred == [(f"inventory-reconcile-{org_id}", {"organization_id": org_id})]


def test_reconciliation_reports_differences_without_fixing_them(tenant_a, caplog):
    with tenant_context(tenant_a.org):
        part = services.create_part(membership=tenant_a.admin, code="ROD", name="Rodamiento")
        services.receive_stock(membership=tenant_a.admin, part=part, quantity=5)
    org_id = str(tenant_a.org.pk)
    with caplog.at_level(logging.WARNING, logger="apps.inventory.tasks"):
        assert inventory_tasks.reconcile_organization_stock(organization_id=org_id) == 0
    assert not caplog.records

    with tenant_context(tenant_a.org):
        # Un descuadre simulado (el saldo solo cambia por movimientos: aquí se fuerza a mano).
        StockItem.objects.filter(part=part).update(on_hand=7, updated_at=timezone.now())
    with caplog.at_level(logging.WARNING, logger="apps.inventory.tasks"):
        assert inventory_tasks.reconcile_organization_stock(organization_id=org_id) == 1
    [record] = caplog.records
    assert record.levelno == logging.WARNING and tenant_a.org.slug in record.getMessage()
    with tenant_context(tenant_a.org):
        assert StockItem.objects.get(part=part).on_hand == 7  # sin --fix: no corrige
        reconcile_stock(fix=True)
        assert StockItem.objects.get(part=part).on_hand == 5


def test_reconciliation_skips_inactive_organizations(tenant_a):
    tenant_a.org.is_active = False
    tenant_a.org.save(update_fields=["is_active"])
    assert inventory_tasks.reconcile_organization_stock(organization_id=str(tenant_a.org.pk)) == 0
