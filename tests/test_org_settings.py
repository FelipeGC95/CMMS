"""Configuración de la organización (WO-02 CA2, REQ-03 CA2)."""

import pytest
from django.urls import reverse

from apps.accounts.permissions import PermissionDeniedError
from apps.assets.models import Asset
from apps.audit.models import AuditEvent
from apps.core.tenancy import tenant_context
from apps.organizations.models import Plan
from apps.organizations.selectors import organization_quotas
from apps.organizations.services import update_organization_settings
from tests.conftest import login

# ---------- Servicio ----------


def test_admin_updates_closing_steps_and_it_is_audited(tenant_a):
    with tenant_context(tenant_a.org):
        update_organization_settings(
            membership=tenant_a.admin, require_close_approval=True, require_failure_details=True
        )
        event = AuditEvent.objects.get(target_id=str(tenant_a.org.pk))
    tenant_a.org.refresh_from_db()
    assert tenant_a.org.require_close_approval is True
    assert tenant_a.org.require_failure_details is True
    assert event.action == AuditEvent.Action.UPDATE
    assert event.actor == tenant_a.admin.user
    assert event.changes == {
        "require_close_approval": [False, True],
        "require_failure_details": [False, True],
    }


def test_saving_without_changes_records_nothing(tenant_a):
    with tenant_context(tenant_a.org):
        update_organization_settings(
            membership=tenant_a.admin, require_close_approval=False, require_failure_details=False
        )
        assert not AuditEvent.objects.filter(target_id=str(tenant_a.org.pk)).exists()


@pytest.mark.parametrize("role", ["planner", "supervisor", "technician", "viewer", "external"])
def test_only_the_admin_can_change_the_settings(tenant_a, role):
    tenant_a.tech.role = role
    tenant_a.tech.save(update_fields=["role"])
    with tenant_context(tenant_a.org), pytest.raises(PermissionDeniedError):
        update_organization_settings(
            membership=tenant_a.tech, require_close_approval=True, require_failure_details=True
        )
    tenant_a.org.refresh_from_db()
    assert tenant_a.org.require_close_approval is False


def test_quotas_count_usage_against_the_plan(tenant_a):
    plan = Plan.objects.create(code="p", name="P", max_assets=1, max_users=10, max_storage_mb=None)
    tenant_a.org.plan = plan
    tenant_a.org.save(update_fields=["plan"])
    with tenant_context(tenant_a.org):
        Asset.objects.create(code="CMP-02", name="Compresor 2", asset_class=tenant_a.asset.asset_class)
        assets, users, storage = organization_quotas(membership=tenant_a.admin)
    assert (assets.used, assets.limit, assets.is_over) == (2, 1, True)
    assert (users.used, users.limit, users.is_over) == (3, 10, False)
    assert (storage.used, storage.limit) == (0, None)


# ---------- Vista ----------


def test_admin_sees_and_saves_the_settings_page(client, tenant_a):
    login(client, tenant_a.admin)
    url = reverse("organization_settings")
    page = client.get(url)
    assert page.status_code == 200
    assert "require_close_approval" in page.content.decode()

    response = client.post(url, {"require_close_approval": "on"})
    assert response.status_code == 302
    tenant_a.org.refresh_from_db()
    assert tenant_a.org.require_close_approval is True
    assert tenant_a.org.require_failure_details is False


def test_menu_links_to_settings_only_for_the_admin(client, tenant_a):
    url = reverse("organization_settings")
    login(client, tenant_a.admin)
    assert url in client.get(reverse("menu")).content.decode()
    login(client, tenant_a.tech)
    assert url not in client.get(reverse("menu")).content.decode()
