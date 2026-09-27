"""Planes preventivos: la organización B no ve, edita ni asigna planes o activos de A."""

import pytest
from django.core.exceptions import ValidationError
from django.urls import reverse

from apps.core.tenancy import tenant_context
from apps.maintenance import services
from apps.maintenance.models import PlanAssignment, PlanVersion
from tests.conftest import login


def test_plan_list_only_shows_own_plans(client, tenant_a, tenant_b):
    with tenant_context(tenant_b.org):
        tenant_b.plan.name = "Plan de B"
        tenant_b.plan.save()
    login(client, tenant_a.admin)
    body = client.get(reverse("plan_list")).content.decode()
    assert "Plan de B" not in body


@pytest.mark.parametrize(
    "name",
    ["plan_detail", "plan_draft", "plan_publish", "plan_discard", "plan_assign"],
)
def test_other_org_plan_pages_are_404(client, tenant_a, tenant_b, name):
    with tenant_context(tenant_a.org):
        services.create_draft(membership=tenant_a.admin, plan=tenant_a.plan)
    login(client, tenant_b.admin)
    assert client.get(reverse(name, args=[tenant_a.plan.pk])).status_code == 404


@pytest.mark.parametrize("name", ["plan_draft_start", "plan_assign_class", "plan_discard", "plan_publish"])
def test_other_org_plan_writes_are_404(client, tenant_a, tenant_b, name):
    login(client, tenant_b.admin)
    assert client.post(reverse(name, args=[tenant_a.plan.pk])).status_code == 404
    with tenant_context(tenant_a.org):
        assert tenant_a.plan.versions.count() == 1
        assert not PlanAssignment.objects.exists()


def test_other_org_draft_items_cannot_be_edited(client, tenant_a, tenant_b):
    with tenant_context(tenant_a.org):
        draft = services.create_draft(membership=tenant_a.admin, plan=tenant_a.plan)
        item = draft.items.first()
    login(client, tenant_b.admin)
    assert client.post(reverse("plan_item_remove", args=[tenant_a.plan.pk, item.pk])).status_code == 404
    # Ni combinando el plan propio con el ítem ajeno.
    with tenant_context(tenant_b.org):
        services.create_draft(membership=tenant_b.admin, plan=tenant_b.plan)
    assert client.post(reverse("plan_item_remove", args=[tenant_b.plan.pk, item.pk])).status_code == 404
    with tenant_context(tenant_a.org):
        assert draft.items.count() == 2


def test_cannot_assign_own_plan_to_other_org_assets(client, tenant_a, tenant_b):
    login(client, tenant_b.admin)
    client.post(reverse("plan_assign", args=[tenant_b.plan.pk]), {"assets": [str(tenant_a.asset.pk)]})
    with tenant_context(tenant_a.org):
        assert not PlanAssignment.objects.exists()
    with tenant_context(tenant_b.org):
        assert not PlanAssignment.objects.exists()


def test_cannot_assign_other_org_plan_from_own_asset(client, tenant_a, tenant_b):
    login(client, tenant_b.admin)
    response = client.post(
        reverse("asset_plan_assign", args=[tenant_b.asset.pk]), {"plan": str(tenant_a.plan.pk)}
    )
    assert response.status_code == 200  # plan inválido en el formulario
    assert client.get(reverse("asset_plan_assign", args=[tenant_a.asset.pk])).status_code == 404
    with tenant_context(tenant_b.org):
        assert not PlanAssignment.objects.exists()


def test_service_rejects_other_org_asset(tenant_a, tenant_b):
    with tenant_context(tenant_b.org):
        with pytest.raises(ValidationError):
            services.assign_plan_to_assets(
                membership=tenant_b.admin, plan=tenant_b.plan, targets=[(tenant_a.asset, None)]
            )


def test_other_org_assignment_cannot_be_deactivated(client, tenant_a, tenant_b):
    with tenant_context(tenant_a.org):
        assignment = services.assign_plan(plan=tenant_a.plan, asset=tenant_a.asset)
    login(client, tenant_b.admin)
    url = reverse("plan_assignment_deactivate", args=[tenant_b.plan.pk, assignment.pk])
    assert client.post(url).status_code == 404
    with tenant_context(tenant_a.org):
        assert PlanAssignment.objects.get(pk=assignment.pk).is_active


def test_class_plans_for_new_assets_stay_in_their_org(tenant_a, tenant_b):
    with tenant_context(tenant_a.org):
        assigned, _warnings = services.assign_class_plans(assets=[tenant_a.asset])
        assert assigned == 1
        assert PlanVersion.objects.count() == 1
    with tenant_context(tenant_b.org):
        assert not PlanAssignment.objects.exists()
