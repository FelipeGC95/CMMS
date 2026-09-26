"""Nombres visibles de los estados de OT configurables por organización (WO-02 CA5)."""

import pytest
from django.core.exceptions import ValidationError
from django.template import Context, Template
from django.urls import reverse

from apps.audit.models import AuditEvent
from apps.core.tenancy import tenant_context
from apps.organizations.services import update_work_order_status_labels
from apps.work.models import WorkOrder
from apps.work.services import report_failure
from tests.conftest import login

# ---------- Servicio ----------


def test_admin_sets_labels_and_it_is_audited(tenant_a):
    with tenant_context(tenant_a.org):
        update_work_order_status_labels(
            membership=tenant_a.admin, labels={"in_progress": "  En   faena ", "on_hold": ""}
        )
        event = AuditEvent.objects.get(target_id=str(tenant_a.org.pk))
    tenant_a.org.refresh_from_db()
    assert tenant_a.org.work_order_status_labels == {"in_progress": "En faena"}
    assert event.action == AuditEvent.Action.UPDATE
    assert event.changes == {"work_order_status_labels.in_progress": ["", "En faena"]}


def test_blank_label_resets_to_the_default(tenant_a):
    with tenant_context(tenant_a.org):
        update_work_order_status_labels(membership=tenant_a.admin, labels={"closed": "Archivada"})
        update_work_order_status_labels(membership=tenant_a.admin, labels={"closed": "   "})
    tenant_a.org.refresh_from_db()
    assert tenant_a.org.work_order_status_labels == {}


def test_saving_the_same_labels_records_nothing(tenant_a):
    with tenant_context(tenant_a.org):
        update_work_order_status_labels(membership=tenant_a.admin, labels={"requested": ""})
        assert not AuditEvent.objects.filter(target_id=str(tenant_a.org.pk)).exists()


@pytest.mark.parametrize(
    "labels",
    [
        {"archived": "Archivada"},
        {"in_progress": "x" * 41},
        {"in_progress": 5},
    ],
)
def test_invalid_labels_are_rejected(tenant_a, labels):
    with tenant_context(tenant_a.org), pytest.raises(ValidationError):
        update_work_order_status_labels(membership=tenant_a.admin, labels=labels)
    tenant_a.org.refresh_from_db()
    assert tenant_a.org.work_order_status_labels == {}


def test_every_status_accepts_a_label(tenant_a):
    labels = {key: f"Estado {i}" for i, key in enumerate(WorkOrder.Status.values)}
    with tenant_context(tenant_a.org):
        update_work_order_status_labels(membership=tenant_a.admin, labels=labels)
    tenant_a.org.refresh_from_db()
    assert tenant_a.org.work_order_status_labels == labels


# ---------- Etiqueta en plantillas ----------


def _render(source, **context):
    return Template("{% load ui %}" + source).render(Context(context))


def test_template_helpers_use_the_organization_label_and_fall_back(tenant_a):
    tenant_a.org.work_order_status_labels = {"in_progress": "En faena"}
    with tenant_context(tenant_a.org):
        assert _render("{{ s|status_label }}", s="in_progress") == "En faena"
        assert _render("{{ s|status_label }}", s="on_hold") == str(WorkOrder.Status.ON_HOLD.label)
        badge = _render("{% status_badge wo %}", wo=WorkOrder(status="in_progress"))
    assert "En faena" in badge and "<svg" in badge
    # Sin organización en contexto: nombre por defecto.
    assert _render("{{ s|status_label }}", s="in_progress") == str(WorkOrder.Status.IN_PROGRESS.label)


# ---------- Vistas ----------


def _work_order(tenant):
    with tenant_context(tenant.org):
        return report_failure(
            membership=tenant.viewer, asset=tenant.asset, description="Falla", asset_stopped=False
        ).work_order


def test_admin_edits_labels_from_settings_and_they_show_on_work_orders(client, tenant_a):
    wo = _work_order(tenant_a)
    login(client, tenant_a.admin)
    url = reverse("organization_settings")
    assert "label-requested" in client.get(url).content.decode()

    response = client.post(url, {"section": "status_labels", "label-requested": "Por revisar"})
    assert response.status_code == 302
    tenant_a.org.refresh_from_db()
    assert tenant_a.org.work_order_status_labels == {"requested": "Por revisar"}
    # Los pasos del cierre no se tocan al guardar los nombres.
    assert tenant_a.org.require_close_approval is False

    assert "Por revisar" in client.get(reverse("work_order_list")).content.decode()
    assert "Por revisar" in client.get(reverse("work_order_detail", args=[wo.pk])).content.decode()


def test_too_long_label_shows_the_form_error(client, tenant_a):
    login(client, tenant_a.admin)
    response = client.post(
        reverse("organization_settings"), {"section": "status_labels", "label-requested": "x" * 41}
    )
    assert response.status_code == 200
    assert response.context["labels_form"].errors
    tenant_a.org.refresh_from_db()
    assert tenant_a.org.work_order_status_labels == {}


def test_saving_closing_steps_keeps_the_labels(client, tenant_a):
    tenant_a.org.work_order_status_labels = {"closed": "Archivada"}
    tenant_a.org.save(update_fields=["work_order_status_labels"])
    login(client, tenant_a.admin)
    client.post(reverse("organization_settings"), {"section": "closing", "require_close_approval": "on"})
    tenant_a.org.refresh_from_db()
    assert tenant_a.org.require_close_approval is True
    assert tenant_a.org.work_order_status_labels == {"closed": "Archivada"}
