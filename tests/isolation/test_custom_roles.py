"""Roles personalizados (ORG-12): la organización A no ve, edita, asigna ni referencia roles de B."""

import pytest
from django.urls import reverse

from apps.accounts import services
from apps.accounts.models import CustomRole, Invitation, Membership
from apps.accounts.selectors import assignable_custom_roles, custom_role_detail, custom_role_list
from apps.core.tenancy import CrossTenantError, tenant_context
from tests.conftest import login

Role = Membership.Role


@pytest.fixture
def role_b(tenant_a, tenant_b):
    with tenant_context(tenant_b.org):
        return services.create_custom_role(
            membership=tenant_b.admin, name="Bodeguero B", base_role=Role.TECHNICIAN
        )


def test_selectors_never_return_roles_of_other_organization(tenant_a, role_b):
    with tenant_context(tenant_a.org):
        services.create_custom_role(membership=tenant_a.admin, name="Bodeguero A", base_role=Role.VIEWER)
        assert [r.name for r in custom_role_list(membership=tenant_a.admin)] == ["Bodeguero A"]
        assert [r.name for r in assignable_custom_roles(membership=tenant_a.admin)] == ["Bodeguero A"]
        with pytest.raises(CustomRole.DoesNotExist):
            custom_role_detail(membership=tenant_a.admin, pk=role_b.pk)


def test_screens_do_not_show_or_touch_roles_of_other_organization(client, tenant_a, role_b):
    login(client, tenant_a.admin)
    assert "Bodeguero B" not in client.get(reverse("custom_role_list")).content.decode()
    assert client.get(reverse("custom_role_edit", args=[role_b.pk])).status_code == 404
    response = client.post(reverse("custom_role_edit", args=[role_b.pk]), {"name": "Hack"})
    assert response.status_code == 404
    response = client.post(reverse("custom_role_active", args=[role_b.pk]), {"active": "0"})
    assert response.status_code == 404
    with tenant_context(role_b.organization):
        role_b.refresh_from_db()
    assert (role_b.name, role_b.is_active) == ("Bodeguero B", True)
    assert "Bodeguero B" not in client.get(reverse("team")).content.decode()


def test_cannot_assign_role_of_other_organization(client, tenant_a, role_b):
    with tenant_context(tenant_a.org):
        with pytest.raises(services.TeamError):
            services.update_member(
                membership=tenant_a.admin,
                target=tenant_a.viewer,
                role=Role.VIEWER,
                custom_role=role_b,
                restricted_to_locations=False,
                locations=[],
            )
        with pytest.raises(services.TeamError):
            services.invite_member(
                membership=tenant_a.admin,
                email="x@empresa-a.cl",
                role=Role.VIEWER,
                custom_role=role_b.pk,
                base_url="https://x.cl/",
            )
    login(client, tenant_a.admin)
    response = client.post(
        reverse("member_edit", args=[tenant_a.viewer.pk]),
        {"role": f"custom:{role_b.pk}", "weekly_capacity_hours": "40"},
    )
    assert response.status_code == 200  # el formulario rechaza la opción
    response = client.post(reverse("team"), {"email": "y@empresa-a.cl", "role": f"custom:{role_b.pk}"})
    assert response.status_code == 200
    assert Membership.objects.get(pk=tenant_a.viewer.pk).custom_role_id is None
    assert not Invitation.objects.filter(custom_role=role_b).exists()


def test_models_reject_cross_organization_reference(tenant_a, role_b):
    membership = Membership.objects.get(pk=tenant_a.viewer.pk)
    membership.custom_role_id = role_b.pk
    with pytest.raises(CrossTenantError):
        membership.save()
    with pytest.raises(CrossTenantError):
        Invitation(
            organization=tenant_a.org,
            email="z@empresa-a.cl",
            role=Role.TECHNICIAN,
            custom_role=role_b,
            token_hash="x" * 64,
            expires_at="2030-01-01T00:00:00Z",
        ).save()


def test_names_are_unique_per_organization_only(tenant_a, role_b):
    with tenant_context(tenant_a.org):
        role = services.create_custom_role(
            membership=tenant_a.admin, name="Bodeguero B", base_role=Role.VIEWER
        )
    assert role.organization_id == tenant_a.org.pk
