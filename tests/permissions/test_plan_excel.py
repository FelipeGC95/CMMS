"""Planes por Excel (PM-08): solo administrador y planificador exportan o importan."""

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from apps.accounts.models import Membership, User
from apps.accounts.permissions import PermissionDeniedError
from apps.core.tenancy import tenant_context
from apps.maintenance.plan_excel import apply_plan_import, build_plan_preview, export_rows
from tests.conftest import login


def _member(tenant, role):
    user = User.objects.create_user(f"{role}-xls@a.cl", "x")
    return Membership.objects.create(user=user, organization=tenant.org, role=role)


@pytest.mark.parametrize("role", ["supervisor", "technician", "viewer", "external"])
def test_other_roles_get_403(client, tenant_a, role):
    member = _member(tenant_a, role)
    login(client, member)
    assert client.get(reverse("plan_export")).status_code == 403
    assert client.get(reverse("plan_import")).status_code == 403
    upload = SimpleUploadedFile("p.xlsx", b"x")
    assert client.post(reverse("plan_import"), {"file": upload}).status_code == 403
    assert client.post(reverse("plan_import_confirm", args=["tok"])).status_code == 403
    assert reverse("plan_import") not in client.get(reverse("menu")).content.decode()
    with tenant_context(tenant_a.org), pytest.raises(PermissionDeniedError):
        export_rows(membership=member)
    with tenant_context(tenant_a.org), pytest.raises(PermissionDeniedError):
        build_plan_preview({"plans": [], "items": [], "resources": []}, membership=member)
    with tenant_context(tenant_a.org), pytest.raises(PermissionDeniedError):
        apply_plan_import({"rows": []}, membership=member)


@pytest.mark.parametrize("role", ["admin", "planner"])
def test_admin_and_planner_can_export_and_open_import(client, tenant_a, role):
    login(client, _member(tenant_a, role))
    assert client.get(reverse("plan_export")).status_code == 200
    assert client.get(reverse("plan_import")).status_code == 200
    assert reverse("plan_import") in client.get(reverse("menu")).content.decode()
