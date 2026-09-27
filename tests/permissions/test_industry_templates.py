"""Plantillas de rubro: solo administrador y planificador pueden verlas y cargarlas."""

import pytest
from django.urls import reverse

from apps.accounts.models import Membership, User
from apps.core.tenancy import tenant_context
from apps.maintenance.models import MaintenancePlan
from tests.conftest import login


def _member(tenant, role):
    user = User.objects.create_user(f"{role}-tpl@a.cl", "x")
    return Membership.objects.create(user=user, organization=tenant.org, role=role)


@pytest.mark.parametrize("role", ["supervisor", "technician", "viewer", "external"])
def test_other_roles_get_403_and_nothing_is_created(client, tenant_a, role):
    login(client, _member(tenant_a, role))
    assert client.get(reverse("industry_templates")).status_code == 403
    assert client.get(reverse("industry_template", args=["fleet"])).status_code == 403
    assert client.post(reverse("industry_template", args=["fleet"])).status_code == 403
    with tenant_context(tenant_a.org):
        assert not MaintenancePlan.objects.filter(code="GRH-C-250H").exists()
    assert reverse("industry_templates") not in client.get(reverse("menu")).content.decode()


@pytest.mark.parametrize("role", ["admin", "planner"])
def test_admin_and_planner_can_load_a_template(client, tenant_a, role):
    login(client, _member(tenant_a, role))
    assert client.get(reverse("industry_template", args=["fleet"])).status_code == 200
    assert client.post(reverse("industry_template", args=["fleet"])).status_code == 302
    with tenant_context(tenant_a.org):
        assert MaintenancePlan.objects.filter(code="GRH-C-250H").exists()
