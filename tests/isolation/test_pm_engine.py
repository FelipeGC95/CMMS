"""Motor preventivo (tipos de ítem, absorción, ajustes, correctiva desde hallazgo):
la organización B no ve, edita ni referencia datos de A."""

import pytest
from django.urls import reverse

from apps.core.tenancy import tenant_context
from apps.maintenance.models import ItemType
from apps.maintenance.services import create_plan
from apps.work.services import create_work_order
from tests.conftest import login


@pytest.fixture(autouse=True)
def _media(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path


def _text_item(tenant):
    with tenant_context(tenant.org):
        plan = create_plan(
            code="PTX", name="Texto", interval_days=30, items=[{"text": "Nota", "item_type": ItemType.TEXT}]
        )
        wo = create_work_order(
            work_type="preventive", title="x", asset=tenant.asset, checklist=list(plan.items.all())
        )
        return wo, wo.checklist.get()


def test_b_cannot_answer_text_or_signature_items_of_a(client, tenant_a, tenant_b):
    wo, item = _text_item(tenant_a)
    login(client, tenant_b.admin)
    url = reverse("checklist_answer", args=[wo.pk, item.pk])
    assert client.post(url, {"value_text": "intruso"}).status_code == 404
    with tenant_context(tenant_a.org):
        item.refresh_from_db()
        assert item.value_text == "" and item.result == ""
