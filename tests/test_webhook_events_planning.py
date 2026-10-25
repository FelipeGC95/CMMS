"""Eventos de webhook de planificación (X-08, P-04): planes preventivos, asignación y
reprogramación de OT y decisiones sobre solicitudes.

Mismo camino que los demás eventos: desde los servicios, al confirmar, con `data` igual al
serializador de la API v1 y solo con el módulo del plan. Sin red: se reemplazan `resolve_host`
y `http_post`, nunca el ORM.
"""

import datetime
import json

import pytest
from django.utils import timezone

from apps.api import serializers as s
from apps.core.tenancy import tenant_context
from apps.maintenance import services as maintenance
from apps.organizations.models import Plan
from apps.webhooks import services
from apps.webhooks.models import Event
from apps.work import services as work

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


def _api_data(serializer, instance):
    from rest_framework.utils.encoders import JSONEncoder

    return json.loads(json.dumps(serializer(instance).data, cls=JSONEncoder))


# --- Planes preventivos -------------------------------------------------------------------------


def test_publishing_a_plan_version_emits_with_version_numbers(
    tenant_a, network, django_capture_on_commit_callbacks
):
    _subscribe(tenant_a, Event.PLAN_VERSION_PUBLISHED)
    plan = tenant_a.plan
    with tenant_context(tenant_a.org):
        draft = maintenance.create_draft(membership=tenant_a.admin, plan=plan)
        with django_capture_on_commit_callbacks(execute=True):
            maintenance.publish_version(version=draft, by=tenant_a.admin.user, change_note="Más presión")
        plan = type(plan).objects.get(pk=plan.pk)
        expected = _api_data(s.MaintenancePlanSerializer, plan)
    assert [call["type"] for call in network] == [Event.PLAN_VERSION_PUBLISHED]
    assert network[0]["change"] == {"version": 2, "previous_version": 1}
    assert network[0]["data"] == expected
    assert expected["current_version"] == 2 and expected["change_note"] == "Más presión"


def test_new_plans_and_bulk_imports_emit_version_one(tenant_a, network, django_capture_on_commit_callbacks):
    _subscribe(tenant_a, Event.PLAN_VERSION_PUBLISHED)
    with tenant_context(tenant_a.org):
        with django_capture_on_commit_callbacks(execute=True):
            maintenance.create_plan(
                code="PM2",
                name="Semanal",
                calendar_interval=7,
                items=[{"text": "Limpiar", "item_type": "check"}],
            )
        with django_capture_on_commit_callbacks(execute=True) as callbacks:
            maintenance.bulk_create_published_plans(
                organization=tenant_a.org,
                plans=[
                    {
                        "code": f"PM{n}",
                        "name": f"Plan {n}",
                        "fields": {"calendar_interval": 30},
                        "items": [{"text": "Revisar", "item_type": "check"}],
                        "resources": [],
                    }
                    for n in (3, 4)
                ],
                change_note="Plantilla",
            )
    assert len(callbacks) == 1  # el lote se despacha una sola vez
    assert [call["data"]["code"] for call in network] == ["PM2", "PM3", "PM4"]
    assert {json.dumps(call["change"]) for call in network} == {'{"version": 1, "previous_version": null}'}


def test_activating_and_deactivating_a_plan(tenant_a, network, django_capture_on_commit_callbacks):
    _subscribe(tenant_a, Event.PLAN_ACTIVATED, Event.PLAN_DEACTIVATED)
    plan = tenant_a.plan

    def update(is_active, name="Mensual"):
        with django_capture_on_commit_callbacks(execute=True):
            maintenance.update_plan(
                membership=tenant_a.admin,
                plan=plan,
                code=plan.code,
                name=name,
                asset_class=plan.asset_class,
                is_active=is_active,
            )

    with tenant_context(tenant_a.org):
        update(False)
        update(False, name="Mensual compresor")  # sin cambio de estado: nada
        update(True)
    assert [(call["type"], call["data"]["is_active"]) for call in network] == [
        (Event.PLAN_DEACTIVATED, False),
        (Event.PLAN_ACTIVATED, True),
    ]


def test_plan_events_need_the_preventive_module(tenant_a, network, django_capture_on_commit_callbacks):
    _subscribe(tenant_a, Event.PLAN_DEACTIVATED, Event.WORK_ORDER_CREATED)
    plan = Plan.objects.create(code="sin-pm", name="Sin PM", features=["assets", "work_orders", "api"])
    type(tenant_a.org).objects.filter(pk=tenant_a.org.pk).update(plan=plan)
    tenant_a.org.refresh_from_db()
    with tenant_context(tenant_a.org):
        assert Event.PLAN_DEACTIVATED not in services.enabled_events(tenant_a.org)
        with django_capture_on_commit_callbacks(execute=True):
            maintenance.update_plan(
                membership=tenant_a.admin,
                plan=tenant_a.plan,
                code="PM1",
                name="Mensual",
                asset_class=tenant_a.plan.asset_class,
                is_active=False,
            )
    assert network == []


# --- OT: asignación y reprogramación ------------------------------------------------------------


def test_assignment_emits_the_user_ids_that_changed(tenant_a, network, django_capture_on_commit_callbacks):
    _subscribe(tenant_a, Event.WORK_ORDER_ASSIGNED)
    tech, admin = tenant_a.tech.user, tenant_a.admin.user
    with tenant_context(tenant_a.org):
        work_order = work.create_work_order(work_type="corrective", title="Fuga", asset=tenant_a.asset)
        with django_capture_on_commit_callbacks(execute=True):
            work.set_assignees(
                work_order=work_order, membership=tenant_a.admin, assigned_to=tech, helpers=[admin]
            )
        with django_capture_on_commit_callbacks(execute=True):  # sin cambios: nada
            work.set_assignees(
                work_order=work_order, membership=tenant_a.admin, assigned_to=tech, helpers=[admin]
            )
        with django_capture_on_commit_callbacks(execute=True):
            work.set_assignees(work_order=work_order, membership=tenant_a.admin, assigned_to=tech, helpers=[])
        work_order.refresh_from_db()
        expected = _api_data(s.WorkOrderSerializer, work_order)
    assert [call["type"] for call in network] == [Event.WORK_ORDER_ASSIGNED] * 2
    assert network[0]["change"] == {
        "assigned_to": {"from": None, "to": str(tech.pk)},
        "helpers": {"from": [], "to": [str(admin.pk)]},
    }
    assert network[1]["change"] == {"helpers": {"from": [str(admin.pk)], "to": []}}
    assert network[1]["data"] == expected and expected["assigned_to"] == str(tech.pk)


def test_scheduling_rescheduling_and_unscheduling(tenant_a, network, django_capture_on_commit_callbacks):
    _subscribe(tenant_a, Event.WORK_ORDER_RESCHEDULED)
    tomorrow = timezone.localdate() + datetime.timedelta(days=1)
    later = tomorrow + datetime.timedelta(days=2)
    with tenant_context(tenant_a.org):
        work_order = work.create_work_order(work_type="corrective", title="Fuga", asset=tenant_a.asset)
        for day in (tomorrow, tomorrow, later):  # el segundo no cambia el día: nada
            with django_capture_on_commit_callbacks(execute=True):
                work.schedule_work_order(work_order=work_order, membership=tenant_a.admin, scheduled_for=day)
        with django_capture_on_commit_callbacks(execute=True):
            work.unschedule_work_order(work_order=work_order, membership=tenant_a.admin)
    assert [call["change"] for call in network] == [
        {"from": None, "to": tomorrow.isoformat()},
        {"from": tomorrow.isoformat(), "to": later.isoformat()},
        {"from": later.isoformat(), "to": None},
    ]
    assert network[1]["data"]["scheduled_for"] == later.isoformat()


# --- Solicitudes: decisiones --------------------------------------------------------------------


def test_request_decisions_emit_their_events(tenant_a, network, django_capture_on_commit_callbacks):
    _subscribe(tenant_a, Event.WORK_REQUEST_ACCEPTED, Event.WORK_REQUEST_REJECTED, Event.WORK_REQUEST_MERGED)
    with tenant_context(tenant_a.org):
        accepted, rejected, merged, into = (
            work.report_failure(membership=tenant_a.tech, description=text, asset=tenant_a.asset)
            for text in ("Ruido", "Duplicado", "Otra vez", "Original")
        )
        with django_capture_on_commit_callbacks(execute=True):
            work.accept_request(work_request=accepted, membership=tenant_a.admin)
            work.reject_request(work_request=rejected, membership=tenant_a.admin, reason="No es falla")
            work.merge_request(work_request=merged, into=into, membership=tenant_a.admin)
        accepted.refresh_from_db()
        expected = _api_data(s.WorkRequestSerializer, accepted)
    assert [call["type"] for call in network] == [
        Event.WORK_REQUEST_ACCEPTED,
        Event.WORK_REQUEST_REJECTED,
        Event.WORK_REQUEST_MERGED,
    ]
    assert network[0]["change"] == {"from": "pending", "to": "accepted"}
    assert network[0]["data"] == expected and expected["work_order"]  # la OT de la solicitud
    assert network[1]["change"] == {"from": "pending", "to": "rejected", "reason": "No es falla"}
    assert network[2]["change"] == {"from": "pending", "to": "merged", "merged_into": str(into.pk)}


def test_rolled_back_decision_sends_nothing(tenant_a, network, django_capture_on_commit_callbacks):
    _subscribe(tenant_a, Event.WORK_REQUEST_REJECTED)
    with tenant_context(tenant_a.org):
        request = work.report_failure(membership=tenant_a.tech, description="Ruido", asset=tenant_a.asset)
        with django_capture_on_commit_callbacks(execute=True):
            with pytest.raises(work.WorkOrderError):
                work.reject_request(work_request=request, membership=tenant_a.admin, reason=" ")
    assert network == []


def test_new_events_are_offered_in_the_form(tenant_a):
    with tenant_context(tenant_a.org):
        offered = services.enabled_events(tenant_a.org)
    for event in (
        Event.WORK_ORDER_ASSIGNED,
        Event.WORK_ORDER_RESCHEDULED,
        Event.WORK_REQUEST_ACCEPTED,
        Event.WORK_REQUEST_REJECTED,
        Event.WORK_REQUEST_MERGED,
        Event.PLAN_VERSION_PUBLISHED,
        Event.PLAN_ACTIVATED,
        Event.PLAN_DEACTIVATED,
    ):
        assert event in offered
