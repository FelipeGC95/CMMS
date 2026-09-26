"""Aislamiento del cierre de correctiva y de la observación por ítem (NF-07)."""

from django.urls import reverse

from apps.assets.models import Asset
from apps.core.tenancy import tenant_context
from apps.work.models import FailureCode, FailureEvent
from apps.work.services import create_work_order, report_failure
from tests.conftest import login


def test_user_of_b_cannot_note_or_complete_on_a(client, tenant_a, tenant_b):
    with tenant_context(tenant_a.org):
        wo = create_work_order(
            work_type="preventive", title="x", asset=tenant_a.asset, checklist=tenant_a.plan.items.all()
        )
        item = wo.checklist.first()
        corrective = report_failure(
            membership=tenant_a.viewer, asset=tenant_a.asset, description="x", asset_stopped=False
        ).work_order
    login(client, tenant_b.admin)
    note_url = reverse("checklist_answer", args=[wo.pk, item.pk])
    assert client.post(note_url, {"note_only": "1", "note": "intruso"}).status_code == 404
    complete_url = reverse("work_order_complete", args=[corrective.pk])
    assert client.post(complete_url, {"labor_minutes": "5", "impact": "none"}).status_code == 404
    with tenant_context(tenant_a.org):
        item.refresh_from_db()
        assert item.note == ""
        assert not FailureEvent.objects.filter(work_order=corrective).exists()


def test_completion_cannot_reference_failure_codes_or_components_of_b(client, tenant_a, tenant_b):
    with tenant_context(tenant_b.org):
        foreign_mode = FailureCode.objects.filter(kind=FailureCode.Kind.MODE).first()
        foreign_component = Asset.objects.create(
            code="B-COMP", name="Motor B", asset_class=tenant_b.asset.asset_class, parent=tenant_b.asset
        )
    with tenant_context(tenant_a.org):
        # A también tiene un componente: el campo se muestra y debe rechazar el de B.
        Asset.objects.create(
            code="A-COMP", name="Motor A", asset_class=tenant_a.asset.asset_class, parent=tenant_a.asset
        )
        corrective = report_failure(
            membership=tenant_a.viewer, asset=tenant_a.asset, description="x", asset_stopped=False
        ).work_order
    login(client, tenant_a.tech)
    url = reverse("work_order_complete", args=[corrective.pk])
    for extra in ({"failure_mode": foreign_mode.pk}, {"component": foreign_component.pk}):
        response = client.post(url, {"labor_minutes": "5", "impact": "none", **extra})
        assert response.status_code == 400, extra
    with tenant_context(tenant_a.org):
        assert not FailureEvent.objects.filter(work_order=corrective).exists()
