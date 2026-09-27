"""REQ-02: solo el administrador enciende el aviso sin cuenta; apagado, el formulario público no existe."""

import pytest
from django.urls import reverse

from apps.accounts.permissions import PermissionDeniedError
from apps.audit.models import AuditEvent
from apps.core.tenancy import tenant_context
from apps.organizations.services import set_anonymous_reports
from apps.work.models import WorkRequest
from tests.conftest import login

ROLES = ["planner", "supervisor", "technician", "viewer", "external"]


def test_admin_enables_it_from_settings_and_it_is_audited(client, tenant_a):
    login(client, tenant_a.admin)
    response = client.post(
        reverse("organization_settings"), {"section": "anonymous", "allow_anonymous_reports": "on"}
    )
    assert response.status_code == 302
    tenant_a.org.refresh_from_db()
    assert tenant_a.org.allow_anonymous_reports
    with tenant_context(tenant_a.org):
        event = AuditEvent.objects.get(changes__has_key="allow_anonymous_reports")
    assert event.changes["allow_anonymous_reports"] == [False, True]
    assert "Avisos por QR sin cuenta" in client.get(reverse("organization_settings")).content.decode()


@pytest.mark.parametrize("role", ROLES)
def test_service_rejects_non_admins(tenant_a, role):
    tenant_a.tech.role = role
    tenant_a.tech.save(update_fields=["role"])
    with tenant_context(tenant_a.org), pytest.raises(PermissionDeniedError):
        set_anonymous_reports(membership=tenant_a.tech, allow_anonymous_reports=True)
    tenant_a.org.refresh_from_db()
    assert not tenant_a.org.allow_anonymous_reports


@pytest.mark.parametrize("role", ROLES)
def test_view_rejects_non_admins(client, tenant_a, role):
    tenant_a.tech.role = role
    tenant_a.tech.save(update_fields=["role"])
    login(client, tenant_a.tech)
    response = client.post(
        reverse("organization_settings"), {"section": "anonymous", "allow_anonymous_reports": "on"}
    )
    assert response.status_code == 403
    tenant_a.org.refresh_from_db()
    assert not tenant_a.org.allow_anonymous_reports


def test_public_form_is_unavailable_when_disabled(client, tenant_a):
    url = reverse("asset_qr", args=[tenant_a.asset.qr_token])
    get = client.get(url)
    post = client.post(url, {"name": "Juan", "description": "x", "answer": 2, "challenge": "x"})
    for response in (get, post):
        assert response.status_code == 302 and reverse("account_login") in response["Location"]
    assert client.get(reverse("asset_qr_sent", args=[tenant_a.asset.qr_token])).status_code == 404
    with tenant_context(tenant_a.org):
        assert not WorkRequest.objects.exists()


@pytest.mark.parametrize("role", ["technician", "viewer"])
def test_inbox_photo_needs_the_manage_permission(client, tenant_a, role):
    from apps.work.services import report_anonymous_failure

    tenant_a.org.allow_anonymous_reports = True
    tenant_a.org.save()
    with tenant_context(tenant_a.org):
        request = report_anonymous_failure(asset=tenant_a.asset, name="Juan", description="x")
    member = tenant_a.tech if role == "technician" else tenant_a.viewer
    login(client, member)
    assert client.get(reverse("request_photo", args=[request.pk])).status_code == 403
