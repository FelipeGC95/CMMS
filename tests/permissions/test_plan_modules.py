"""ORG-10 CA2: un módulo fuera del plan responde 403 a todos los roles, también al administrador."""

import pytest
from django.urls import reverse

from apps.organizations.models import Plan
from tests.conftest import login


@pytest.mark.parametrize("role", ["admin", "tech", "viewer"])
def test_module_outside_plan_is_forbidden_for_every_role(client, tenant_a, role):
    tenant_a.org.plan = Plan.objects.create(code="solo-activos", name="Solo activos", features=["assets"])
    tenant_a.org.save(update_fields=["plan"])
    login(client, getattr(tenant_a, role))
    assert client.get(reverse("work_order_list")).status_code == 403
    assert client.get(reverse("asset_list")).status_code == 200
