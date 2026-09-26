"""Solo el administrador de la organización cambia los nombres de los estados de OT (WO-02 CA5)."""

import pytest
from django.urls import reverse

from apps.accounts.permissions import PermissionDeniedError
from apps.core.tenancy import tenant_context
from apps.organizations.services import update_work_order_status_labels
from tests.conftest import login

ROLES = ["planner", "supervisor", "technician", "viewer", "external"]


@pytest.mark.parametrize("role", ROLES)
def test_service_rejects_non_admins(tenant_a, role):
    tenant_a.tech.role = role
    tenant_a.tech.save(update_fields=["role"])
    with tenant_context(tenant_a.org), pytest.raises(PermissionDeniedError):
        update_work_order_status_labels(membership=tenant_a.tech, labels={"requested": "X"})
    tenant_a.org.refresh_from_db()
    assert tenant_a.org.work_order_status_labels == {}


@pytest.mark.parametrize("role", ROLES)
def test_view_rejects_non_admins(client, tenant_a, role):
    tenant_a.tech.role = role
    tenant_a.tech.save(update_fields=["role"])
    login(client, tenant_a.tech)
    response = client.post(
        reverse("organization_settings"), {"section": "status_labels", "label-requested": "X"}
    )
    assert response.status_code == 403
    tenant_a.org.refresh_from_db()
    assert tenant_a.org.work_order_status_labels == {}
