"""Concurrencia del inventario (ADR-028): dos hilos sobre el mismo repuesto no descuadran el saldo.

Solo en PostgreSQL (el CI): SQLite no tiene `SELECT … FOR UPDATE` ni escrituras concurrentes.
"""

import threading
from decimal import Decimal

import pytest
from django.db import connection, connections

from apps.core.tenancy import tenant_context
from apps.inventory import services
from apps.inventory.models import StockItem, StockMovement
from tests.conftest import make_tenant
from tests.inventory_utils import make_work_order

pytestmark = [
    pytest.mark.skipif(connection.vendor != "postgresql", reason="bloqueos de fila solo en PostgreSQL"),
    pytest.mark.django_db(transaction=True),
]

ROUNDS = 15


def _in_thread(organization, work, errors):
    try:
        with tenant_context(organization):
            work()
    except Exception as error:  # noqa: BLE001 - se informa en el hilo principal
        errors.append(error)
    finally:
        connections.close_all()


def test_concurrent_movements_keep_balance_equal_to_ledger():
    tenant = make_tenant("concurrente")
    with tenant_context(tenant.org):
        part = services.create_part(membership=tenant.admin, code="ROD", name="Rodamiento")
        main = services.ensure_default_warehouse()
        other = services.create_warehouse(membership=tenant.admin, code="B2", name="Bodega 2")
        work_order = make_work_order(tenant)
        services.reserve_for_work_order(work_order=work_order, part=part, quantity=ROUNDS, source="manual")

    def receiver():
        for i in range(ROUNDS):
            services.receive_stock(membership=tenant.admin, part=part, quantity=2, unit_cost=str(100 + i))
            services.transfer_stock(
                membership=tenant.admin, part=part, from_warehouse=main, to_warehouse=other, quantity=1
            )

    def consumer():
        for _i in range(ROUNDS):
            services.issue_to_work_order(work_order=work_order, part=part, quantity=1, by=tenant.tech.user)
            services.reserve_for_work_order(work_order=work_order, part=part, quantity=1, source="manual")

    errors = []
    threads = [
        threading.Thread(target=_in_thread, args=(tenant.org, job, errors)) for job in (receiver, consumer)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []

    with tenant_context(tenant.org):
        assert services.reconcile_stock() == []
        assert StockItem.objects.get(part=part, warehouse=main).on_hand == Decimal(
            ROUNDS * 2 - ROUNDS - ROUNDS
        )
        assert StockItem.objects.get(part=part, warehouse=other).on_hand == Decimal(ROUNDS)
        # El saldo resultante de cada movimiento sigue el orden en que se aplicó (sin carreras).
        running = Decimal(0)
        for movement in StockMovement.objects.filter(warehouse=main).order_by("created_at"):
            running += movement.quantity
            assert movement.on_hand_after == running
        assert StockItem.objects.get(part=part, warehouse=main).reserved == Decimal(ROUNDS)
