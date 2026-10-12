"""Permisos de la condición en la ficha del activo (C4, CBM-08 CA1/CA2): dar de alta, editar y
desactivar variables pide `asset.manage`; leer, `meter_reading.create`; la sección "Condición"
pide `condition.view` y el módulo `condition` (sin él, las variables siguen como medidores)."""

import pytest
from django.urls import reverse

from apps.accounts.models import Membership
from apps.accounts.permissions import Perm, PermissionDeniedError, has_perm
from apps.assets.services import create_variable
from apps.core.tenancy import tenant_context
from apps.organizations.models import Plan
from tests.condition_utils import variable
from tests.conftest import login
from tests.inventory_utils import add_member

Role = Membership.Role
MANAGERS = {Role.ADMIN, Role.PLANNER}


def _member(tenant, role):
    return {
        Role.ADMIN: tenant.admin,
        Role.TECHNICIAN: tenant.tech,
        Role.VIEWER: tenant.viewer,
    }.get(role) or add_member(tenant, role)


@pytest.fixture
def meter(tenant_a):
    with tenant_context(tenant_a.org):
        return variable(tenant_a.asset, name="Rodamiento")


@pytest.mark.parametrize("role", list(Role))
def test_only_asset_managers_create_edit_and_deactivate_variables(client, tenant_a, meter, role):
    member = _member(tenant_a, role)
    login(client, member)
    allowed = role in MANAGERS
    create = reverse("asset_variable_create", args=[tenant_a.asset.pk])
    edit = reverse("asset_variable_edit", args=[tenant_a.asset.pk, meter.pk])
    active = reverse("asset_variable_active", args=[tenant_a.asset.pk, meter.pk])
    assert (client.get(create).status_code == 200) is allowed
    assert (client.get(edit).status_code == 200) is allowed
    response = client.post(create, {"kind": "vibration", "name": "Motor"})
    assert response.status_code == (302 if allowed else 403)
    assert client.post(active, {"is_active": "0"}).status_code == (302 if allowed else 403)
    with tenant_context(tenant_a.org):
        meter.refresh_from_db()
        assert meter.is_active is not allowed


def test_service_requires_asset_manage(in_a):
    with pytest.raises(PermissionDeniedError):
        create_variable(membership=in_a.tech, asset=in_a.asset, kind="temperature", name="x")


@pytest.mark.parametrize("role", list(Role))
def test_reading_a_variable_follows_meter_reading_create(client, tenant_a, meter, role):
    member = _member(tenant_a, role)
    login(client, member)
    url = reverse("meter_reading", args=[tenant_a.asset.pk, meter.pk])
    response = client.post(url, {f"{meter.pk}-value": "-4"})
    allowed = has_perm(member, Perm.METER_READING_CREATE)
    assert response.status_code == (302 if allowed else 403)
    with tenant_context(tenant_a.org):
        assert meter.readings.exists() is allowed


@pytest.mark.parametrize("role", list(Role))
def test_condition_section_requires_condition_view_and_hides_manage_actions(client, tenant_a, meter, role):
    member = _member(tenant_a, role)
    login(client, member)
    response = client.get(reverse("asset_detail", args=[tenant_a.asset.pk]))
    if response.status_code != 200:
        return  # sin acceso a la ficha (alcance del rol)
    body = response.content.decode()
    assert ('id="condition"' in body) is has_perm(member, Perm.CONDITION_VIEW)
    edit = reverse("asset_variable_edit", args=[tenant_a.asset.pk, meter.pk])
    assert (edit in body) is (role in MANAGERS)
    assert (reverse("asset_variable_create", args=[tenant_a.asset.pk]) in body) is (role in MANAGERS)
    assert (f"{reverse('condition_rule_create')}?meter={meter.pk}" in body) is has_perm(
        member, Perm.CONDITION_MANAGE
    )


def test_without_the_module_the_section_is_hidden_and_variables_stay_as_meters(client, tenant_a, meter):
    tenant_a.org.plan = Plan.objects.get(code="basic")
    tenant_a.org.save(update_fields=["plan"])
    login(client, tenant_a.admin)
    body = client.get(reverse("asset_detail", args=[tenant_a.asset.pk])).content.decode()
    assert 'id="condition"' not in body and "chart-trend" not in body
    assert f'id="meter-{meter.pk}"' in body and "Rodamiento" in body  # sigue en "Medidores"
    assert reverse("meter_reading", args=[tenant_a.asset.pk, meter.pk]) in body
    create = reverse("asset_variable_create", args=[tenant_a.asset.pk])
    assert create not in body
    response = client.get(create)
    assert response.status_code == 403 and "plan" in response.content.decode().lower()
    # Las lecturas se siguen registrando; editar o desactivar la variable sigue disponible.
    reading = reverse("meter_reading", args=[tenant_a.asset.pk, meter.pk])
    assert client.post(reading, {f"{meter.pk}-value": "-2"}).status_code == 302
    assert client.get(reverse("asset_variable_edit", args=[tenant_a.asset.pk, meter.pk])).status_code == 200
    with tenant_context(tenant_a.org):
        assert meter.readings.count() == 1
