"""Eventos de webhook de F3 (X-08): estados de OT, activos, lecturas, condición e inventario.

Cada evento sale una vez, al confirmar la transacción, con `data` igual al serializador de la
API v1; sin el módulo del plan no se ofrece ni se envía. Sin red: se reemplazan
`resolve_host` y `http_post`, nunca el ORM.
"""

import json
from decimal import Decimal

import pytest
from django.db import transaction

from apps.api import serializers as s
from apps.assets import services as assets
from apps.assets.models import Asset, MeterKind
from apps.condition import services as condition
from apps.condition.models import ConditionAlert, ConditionRule
from apps.core.tenancy import tenant_context
from apps.inventory import services as inventory
from apps.inventory.models import StockItem
from apps.organizations.models import Plan
from apps.webhooks import services
from apps.webhooks.models import Event, WebhookDelivery
from apps.work.services import create_work_order, transition
from tests.condition_utils import read, variable

pytestmark = pytest.mark.django_db


@pytest.fixture
def network(monkeypatch):
    calls = []
    monkeypatch.setattr(services, "resolve_host", lambda host, port: ["93.184.216.34"])

    def post(url, *, address, body, headers):
        calls.append(json.loads(body))
        return 200, "ok"

    monkeypatch.setattr(services, "http_post", post)
    return calls


def _subscribe(tenant, *events):
    with tenant_context(tenant.org):
        return services.create_endpoint(
            membership=tenant.admin, name="ERP", url="https://erp.example.com/hooks", events=list(events)
        )


def _api_data(serializer, instance, **context):
    """Lo que devuelve la API v1 para el objeto, como JSON."""
    from rest_framework.utils.encoders import JSONEncoder

    return json.loads(json.dumps(serializer(instance, context=context).data, cls=JSONEncoder))


def _types(calls):
    return [call["type"] for call in calls]


# --- OT: cambios de estado ----------------------------------------------------------------------


def test_status_changes_emit_one_event_each_with_from_and_to(
    tenant_a, network, django_capture_on_commit_callbacks
):
    _subscribe(tenant_a, Event.WORK_ORDER_STATUS_CHANGED, Event.WORK_ORDER_COMPLETED)
    with tenant_context(tenant_a.org):
        work_order = create_work_order(work_type="corrective", title="Fuga", asset=tenant_a.asset)
        with django_capture_on_commit_callbacks(execute=True):
            transition(work_order=work_order, to_status="in_progress", membership=tenant_a.admin)
        assert [c["change"] for c in network] == [{"from": "planned", "to": "in_progress"}]
        network.clear()
        with django_capture_on_commit_callbacks(execute=True):
            transition(work_order=work_order, to_status="on_hold", membership=tenant_a.admin)
            transition(work_order=work_order, to_status="in_progress", membership=tenant_a.admin)
        assert [c["change"]["to"] for c in network] == ["on_hold", "in_progress"]
        network.clear()
        # Completar sin aprobación de cierre: completed, el cierre automático y work_order.completed.
        with django_capture_on_commit_callbacks(execute=True):
            transition(work_order=work_order, to_status="completed", membership=tenant_a.admin)
        work_order.refresh_from_db()
        expected = _api_data(s.WorkOrderSerializer, work_order)
    assert _types(network) == [
        Event.WORK_ORDER_STATUS_CHANGED,
        Event.WORK_ORDER_COMPLETED,
        Event.WORK_ORDER_STATUS_CHANGED,
    ]
    assert [c.get("change") for c in network] == [
        {"from": "in_progress", "to": "completed"},
        None,
        {"from": "completed", "to": "closed"},
    ]
    # `data` es el estado al confirmar (ya cerrada), igual a la API v1.
    assert all(call["data"] == expected for call in network)


def test_cancel_emits_status_changed_and_rollback_emits_nothing(
    tenant_a, network, django_capture_on_commit_callbacks
):
    _subscribe(tenant_a, Event.WORK_ORDER_STATUS_CHANGED)
    with tenant_context(tenant_a.org):
        work_order = create_work_order(work_type="corrective", title="Fuga", asset=tenant_a.asset)
        with django_capture_on_commit_callbacks(execute=True):
            with pytest.raises(RuntimeError), transaction.atomic():
                transition(work_order=work_order, to_status="cancelled", membership=tenant_a.admin)
                raise RuntimeError
        assert network == []
        work_order.refresh_from_db()
        with django_capture_on_commit_callbacks(execute=True):
            transition(work_order=work_order, to_status="cancelled", membership=tenant_a.admin)
    assert [c["change"] for c in network] == [{"from": "planned", "to": "cancelled"}]


# --- Activos y lecturas ---------------------------------------------------------------------------


def test_asset_created_updated_and_status_changed(tenant_a, network, django_capture_on_commit_callbacks):
    _subscribe(tenant_a, Event.ASSET_CREATED, Event.ASSET_UPDATED, Event.ASSET_STATUS_CHANGED)
    with tenant_context(tenant_a.org):
        with django_capture_on_commit_callbacks(execute=True):
            asset = assets.create_asset(
                membership=tenant_a.admin,
                code="CMP-02",
                name="Compresor 2",
                asset_class=tenant_a.asset.asset_class,
            )
        created = _api_data(s.AssetSerializer, Asset.objects.get(pk=asset.pk))
        with django_capture_on_commit_callbacks(execute=True):
            assets.update_asset(
                membership=tenant_a.admin,
                asset=asset,
                code="CMP-02",
                name="Compresor dos",
                asset_class=asset.asset_class,
            )
            # Guardar sin cambios no emite.
            assets.update_asset(
                membership=tenant_a.admin,
                asset=asset,
                code="CMP-02",
                name="Compresor dos",
                asset_class=asset.asset_class,
            )
        with django_capture_on_commit_callbacks(execute=True):
            assets.set_asset_status(membership=tenant_a.admin, asset=asset, status="stopped", reason="Ruido")
    assert _types(network) == [Event.ASSET_CREATED, Event.ASSET_UPDATED, Event.ASSET_STATUS_CHANGED]
    assert network[0]["data"] == created
    assert network[1]["change"] == {"fields": ["name"]}
    assert network[1]["data"]["name"] == "Compresor dos"
    assert network[2]["change"] == {"from": "operational", "to": "stopped"}
    assert network[2]["data"]["status"] == "stopped"


def test_meter_reading_created_includes_condition_variables(
    tenant_a, network, django_capture_on_commit_callbacks
):
    _subscribe(tenant_a, Event.METER_READING_CREATED)
    with tenant_context(tenant_a.org):
        meter = variable(tenant_a.asset)
        with django_capture_on_commit_callbacks(execute=True):
            reading = read(
                meter, "65.5", membership=tenant_a.admin, client_uuid="7d3f7c3e-1111-4c2a-9a10-000000000001"
            )
            # Reenviar el mismo client_uuid no crea otra lectura ni otro evento.
            read(meter, "65.5", membership=tenant_a.admin, client_uuid="7d3f7c3e-1111-4c2a-9a10-000000000001")
        expected = _api_data(s.MeterReadingSerializer, reading)
    assert _types(network) == [Event.METER_READING_CREATED]
    assert network[0]["data"] == expected and network[0]["data"]["value"] == "65.500"


def test_bulk_reading_import_dispatches_one_batch(tenant_a, network, django_capture_on_commit_callbacks):
    """La carga masiva crea un evento por lectura en un solo despacho al final (sin bloquear)."""
    from apps.assets.models import Meter
    from apps.assets.reading_imports import apply_reading_import, build_reading_preview

    _subscribe(tenant_a, Event.METER_READING_CREATED)
    with tenant_context(tenant_a.org):
        Meter.objects.create(asset=tenant_a.asset, kind=MeterKind.HOURS)
        rows = [
            {"asset_code": "CMP-01", "value": str(10 * i), "read_at": f"2026-09-{i:02d}", "_row": i}
            for i in range(1, 6)
        ]
        preview = build_reading_preview(rows, membership=tenant_a.admin)
        with django_capture_on_commit_callbacks(execute=False) as callbacks:
            result = apply_reading_import(
                [r.__dict__ for r in preview.valid_rows], membership=tenant_a.admin, source="import"
            )
        assert result.created == 5
        assert len(callbacks) == 1  # un solo despacho para las 5 lecturas
        callbacks[0]()
    assert _types(network) == [Event.METER_READING_CREATED] * 5


def test_batch_with_queue_goes_to_the_worker_in_one_task(
    tenant_a, network, settings, monkeypatch, django_capture_on_commit_callbacks
):
    from apps.webhooks import tasks

    settings.WEBHOOKS_USE_QUEUE = True
    deferred = []
    monkeypatch.setattr(tasks.dispatch_events_task, "defer", lambda **kwargs: deferred.append(kwargs))
    _subscribe(tenant_a, Event.ASSET_UPDATED)
    with tenant_context(tenant_a.org), django_capture_on_commit_callbacks(execute=True):
        with services.collect_events():
            for name in ("Uno", "Dos", "Tres"):
                assets.update_asset(
                    membership=tenant_a.admin,
                    asset=tenant_a.asset,
                    code="CMP-01",
                    name=name,
                    asset_class=tenant_a.asset.asset_class,
                )
    assert network == [] and len(deferred) == 1
    assert deferred[0]["organization_id"] == str(tenant_a.org.pk)
    assert [event[0] for event in deferred[0]["events"]] == [Event.ASSET_UPDATED] * 3
    # El worker crea las entregas (que luego encola, una por destino y evento).
    monkeypatch.setattr(services, "_enqueue", lambda *, delivery, delay: None)
    assert tasks.dispatch_events_task(**deferred[0]) == 3


def test_without_worker_large_batches_are_capped(
    tenant_a, network, monkeypatch, django_capture_on_commit_callbacks
):
    monkeypatch.setattr(services, "SYNC_DELIVERY_LIMIT", 2)
    _subscribe(tenant_a, Event.ASSET_UPDATED)
    with tenant_context(tenant_a.org):
        with django_capture_on_commit_callbacks(execute=True), services.collect_events():
            for name in ("Uno", "Dos", "Tres"):
                assets.update_asset(
                    membership=tenant_a.admin,
                    asset=tenant_a.asset,
                    code="CMP-01",
                    name=name,
                    asset_class=tenant_a.asset.asset_class,
                )
        statuses = list(WebhookDelivery.objects.order_by("created_at").values_list("status", flat=True))
    assert len(network) == 2
    assert statuses == ["succeeded", "succeeded", "failed"]  # la tercera se puede reenviar a mano


# --- Condición ---------------------------------------------------------------------------------


def test_condition_alert_lifecycle_events(in_a, network, django_capture_on_commit_callbacks):
    _subscribe(
        in_a,
        Event.CONDITION_ALERT_OPENED,
        Event.CONDITION_ALERT_ACKNOWLEDGED,
        Event.CONDITION_ALERT_CLOSED,
    )
    meter = variable(in_a.asset)
    rule = condition.create_rule(membership=in_a.admin, meter=meter, operator="gt", threshold="80")
    assert rule.action == ConditionRule.Action.ALERT_WORK_ORDER
    with django_capture_on_commit_callbacks(execute=True):
        read(meter, "95", hours=1)
    alert = ConditionAlert.objects.get()
    with django_capture_on_commit_callbacks(execute=True):
        condition.acknowledge_alert(membership=in_a.admin, alert=alert)
        condition.acknowledge_alert(membership=in_a.admin, alert=alert)  # repetir no emite
    with django_capture_on_commit_callbacks(execute=True):
        transition(work_order=alert.work_order, to_status="cancelled", membership=in_a.admin)
    assert _types(network) == [
        Event.CONDITION_ALERT_OPENED,
        Event.CONDITION_ALERT_ACKNOWLEDGED,
        Event.CONDITION_ALERT_CLOSED,
    ]
    assert network[1]["change"] == {"from": "open", "to": "acknowledged"}
    assert network[2]["change"] == {"from": "acknowledged", "to": "closed"}
    from apps.condition.selectors import alert_list

    expected = _api_data(s.ConditionAlertSerializer, alert_list(membership=in_a.admin).get(pk=alert.pk))
    assert network[2]["data"] == expected


def test_condition_events_need_the_condition_module(tenant_a, network, django_capture_on_commit_callbacks):
    tenant_a.org.plan = Plan.objects.create(
        code="sin-cond", name="Sin condición", features=["api", "assets", "work_orders", "requests"]
    )
    tenant_a.org.save()
    with tenant_context(tenant_a.org):
        assert Event.CONDITION_ALERT_OPENED not in services.enabled_events(tenant_a.org)
        assert Event.PART_BELOW_MINIMUM not in services.enabled_events(tenant_a.org)
        with pytest.raises(services.WebhookError):  # no se ofrece: no se puede suscribir
            services.create_endpoint(
                membership=tenant_a.admin,
                name="x",
                url="https://erp.example.com/",
                events=[Event.CONDITION_ALERT_OPENED],
            )
        endpoint = _subscribe(tenant_a, Event.WORK_ORDER_CREATED)
    # Un destino suscrito antes de perder el módulo no recibe esos eventos.
    from apps.webhooks.models import WebhookEndpoint

    with tenant_context(tenant_a.org):
        WebhookEndpoint.objects.filter(pk=endpoint.pk).update(
            events=[Event.WORK_ORDER_CREATED, Event.STOCK_MOVEMENT_CREATED]
        )
        with django_capture_on_commit_callbacks(execute=True):
            services.dispatch_events(
                [(Event.STOCK_MOVEMENT_CREATED, "assets.Asset", str(tenant_a.asset.pk), None)]
            )
        assert not WebhookDelivery.objects.exists()
    assert network == []


# --- Inventario --------------------------------------------------------------------------------


def test_stock_movement_and_below_minimum_once_per_episode(in_a, network, django_capture_on_commit_callbacks):
    _subscribe(in_a, Event.STOCK_MOVEMENT_CREATED, Event.PART_BELOW_MINIMUM)
    part = inventory.create_part(membership=in_a.admin, code="ROD-6204", name="Rodamiento 6204")
    inventory.receive_stock(membership=in_a.admin, part=part, quantity="10", unit_cost="1000")
    inventory.set_stock_levels(membership=in_a.admin, part=part, min_quantity="5")
    network.clear()
    with django_capture_on_commit_callbacks(execute=True):
        inventory.count_stock(membership=in_a.admin, part=part, counted_quantity="4", note="Conteo")
    item = StockItem.objects.select_related("part", "warehouse").get(part=part)
    assert _types(network) == [Event.STOCK_MOVEMENT_CREATED, Event.PART_BELOW_MINIMUM]
    assert network[0]["data"]["quantity"] == "-6.000"
    assert network[0]["data"]["unit_cost"] is not None  # costos como los ve el token de servicio
    assert network[1]["data"] == _api_data(s.StockItemSerializer, item)
    assert network[1]["data"]["below_min"] is True
    network.clear()
    # Sigue bajo mínimo: otro movimiento no repite el aviso (una vez por episodio).
    with django_capture_on_commit_callbacks(execute=True):
        inventory.count_stock(membership=in_a.admin, part=part, counted_quantity="3", note="Conteo")
    assert _types(network) == [Event.STOCK_MOVEMENT_CREATED]
    network.clear()
    # Sale del episodio y vuelve a caer: aviso de nuevo.
    with django_capture_on_commit_callbacks(execute=True):
        inventory.receive_stock(membership=in_a.admin, part=part, quantity=Decimal("10"))
        inventory.count_stock(membership=in_a.admin, part=part, counted_quantity="2", note="Conteo")
    assert _types(network) == [
        Event.STOCK_MOVEMENT_CREATED,
        Event.STOCK_MOVEMENT_CREATED,
        Event.PART_BELOW_MINIMUM,
    ]


def test_destinations_keep_their_events_and_only_get_subscribed_ones(
    tenant_a, network, django_capture_on_commit_callbacks
):
    """Los destinos de antes siguen con sus eventos v1; los nuevos se eligen por destino."""
    old = _subscribe(tenant_a, Event.WORK_ORDER_CREATED)
    _subscribe(tenant_a, Event.ASSET_UPDATED)
    with tenant_context(tenant_a.org):
        with django_capture_on_commit_callbacks(execute=True):
            create_work_order(work_type="corrective", title="Fuga", asset=tenant_a.asset)
            assets.update_asset(
                membership=tenant_a.admin,
                asset=tenant_a.asset,
                code="CMP-01",
                name="Otro nombre",
                asset_class=tenant_a.asset.asset_class,
            )
        by_endpoint = dict(WebhookDelivery.objects.values_list("endpoint_id", "event_type"))
    assert by_endpoint[old.pk] == Event.WORK_ORDER_CREATED
    assert sorted(_types(network)) == sorted([Event.WORK_ORDER_CREATED, Event.ASSET_UPDATED])
