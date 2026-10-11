"""Aislamiento de los repuestos del catálogo en planes (INV-11) y de la reserva automática (INV-10)."""

from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.urls import reverse

from apps.core.tenancy import tenant_context
from apps.inventory import services as inventory
from apps.inventory.from_plans import create_parts_from_plan_resources, plan_part_proposals
from apps.inventory.models import Part, Reservation
from apps.maintenance import plan_excel
from apps.maintenance import services as plans
from apps.maintenance.models import PlanResource
from tests.conftest import login

pytestmark = pytest.mark.usefixtures("reconciled_inventory")
PART = PlanResource.Kind.PART


def _plan(code, resources):
    return plans.create_plan(
        code=code,
        name=code,
        calendar_interval=30,
        items=[{"text": "x", "item_type": "check"}],
        resources=resources,
    )


@pytest.fixture
def b_side(tenant_b):
    with tenant_context(tenant_b.org):
        part = inventory.create_part(membership=tenant_b.admin, code="FIL-B", name="Filtro de aceite")
        plan = _plan("PB", [{"kind": PART, "description": "Correa de B", "quantity": Decimal(1), "unit": ""}])
        resource = plan.current_version.resources.get()
    return {"part": part, "plan": plan, "resource": resource}


def test_resources_cannot_reference_a_part_of_another_org(in_a, b_side):
    draft = plans.create_draft(membership=in_a.admin, plan=in_a.plan)
    with pytest.raises(ValidationError):
        plans.add_draft_resource(
            membership=in_a.admin,
            version=draft,
            kind=PART,
            description="",
            quantity=1,
            part=b_side["part"].pk,
        )
    own = plans.add_draft_resource(
        membership=in_a.admin, version=draft, kind=PART, description="x", quantity=1
    )
    with pytest.raises(ValidationError):
        plans.update_draft_resource(
            membership=in_a.admin,
            resource=own,
            kind=PART,
            description="x",
            quantity=1,
            part=b_side["part"].pk,
        )
    published = _plan("PA", [{"kind": PART, "description": "y", "quantity": Decimal(1), "unit": ""}])
    with pytest.raises(ValidationError):
        plans.link_published_resource(
            membership=in_a.admin, resource=published.current_version.resources.get(), part=b_side["part"].pk
        )
    assert not PlanResource.objects.filter(part__isnull=False).exists()


def test_create_from_plans_only_sees_own_resources(in_a, b_side):
    _plan("PA", [{"kind": PART, "description": "Correa de A", "quantity": Decimal(1), "unit": ""}])

    assert [p.key for p in plan_part_proposals(membership=in_a.admin)] == ["correa de a"]
    assert create_parts_from_plan_resources(membership=in_a.admin) == (1, 1)

    with tenant_context(b_side["part"].organization):
        assert PlanResource.objects.get(pk=b_side["resource"].pk).part_id is None
        assert Part.objects.count() == 1
    # Un repuesto de B con el mismo nombre no se reutiliza en A.
    assert Part.objects.get(code="REP-0001").name == "Correa de A"


def test_screens_do_not_show_or_accept_another_orgs_data(client, in_a, b_side):
    draft = plans.create_draft(membership=in_a.admin, plan=in_a.plan)
    login(client, in_a.admin)

    search = client.get(reverse("inventory_plan_part_search"), {"part_q": "filtro"}).content.decode()
    assert "FIL-B" not in search
    # Elegido "a mano" con el UUID de B: el buscador no lo muestra y el formulario lo rechaza.
    search = client.get(
        reverse("inventory_plan_part_search"), {"selected": b_side["part"].pk}
    ).content.decode()
    assert str(b_side["part"].pk) not in search
    response = client.post(
        reverse("plan_draft", args=[in_a.plan.pk]),
        {
            "action": "resource",
            "resource-kind": PART,
            "resource-quantity": "1",
            "resource-part": b_side["part"].pk,
        },
    )
    assert response.status_code == 200 and not draft.resources.exists()
    # Enlazar un recurso de B por la URL de un plan de A (o de B) no existe.
    assert (
        client.get(reverse("plan_resource_link", args=[in_a.plan.pk, b_side["resource"].pk])).status_code
        == 404
    )
    assert (
        client.get(reverse("plan_resource_link", args=[b_side["plan"].pk, b_side["resource"].pk])).status_code
        == 404
    )
    published = _plan("PA", [{"kind": PART, "description": "y", "quantity": Decimal(1), "unit": ""}])
    resource = published.current_version.resources.get()
    response = client.post(
        reverse("plan_resource_link", args=[published.pk, resource.pk]), {"part": b_side["part"].pk}
    )
    assert response.status_code == 200
    resource.refresh_from_db()
    assert resource.part_id is None
    assert "Correa de B" not in client.get(reverse("inventory_from_plans")).content.decode()


def test_plan_excel_import_does_not_resolve_another_orgs_part_code(in_a, b_side):
    raw = {
        "plans": [{"code": "PX", "name": "PX", "calendar_interval": "30", "_row": 2}],
        "items": [{"plan": "PX", "text": "x", "_row": 2}],
        "resources": [{"plan": "PX", "description": "Filtro", "part": "FIL-B", "_row": 2}],
    }
    [row] = plan_excel.build_plan_preview(raw, membership=in_a.admin).rows
    assert row.action == "error"


def test_generation_reserves_only_in_its_own_org(in_a, tenant_b, b_side):
    part = inventory.create_part(membership=in_a.admin, code="FIL-A", name="Filtro A")
    plan = _plan("PA", [{"kind": PART, "description": "f", "quantity": Decimal(1), "unit": "", "part": part}])
    plans.assign_plan(plan=plan, asset=in_a.asset, starts_on=in_a.asset.created_at.date())

    [wo] = plans.generate_due_work_orders(horizon_days=14)

    assert Reservation.objects.get(work_order=wo).part_id == part.pk
    with tenant_context(tenant_b.org):
        assert not Reservation.objects.exists()
