"""Alta y edición de activos (AST-02) y jerarquía sin ciclos (AST-03 CA1)."""

import datetime

import pytest
from django.core.exceptions import ValidationError
from django.urls import reverse

from apps.accounts.permissions import PermissionDeniedError
from apps.assets.models import Asset, AssetClass, AssetStatusPeriod, AttributeType
from apps.assets.services import add_class_attribute, create_asset, creates_cycle, update_asset
from apps.audit.models import AuditEvent
from apps.core.tenancy import platform_context, tenant_context
from apps.organizations.models import Location
from apps.work.services import report_failure
from tests.conftest import login


def _fields(asset, **overrides):
    values = {
        "code": asset.code,
        "name": asset.name,
        "asset_class": asset.asset_class,
        "location": asset.location,
        "parent": asset.parent,
        "criticality": asset.criticality,
        "manufacturer": asset.manufacturer,
        "model": asset.model,
        "serial_number": asset.serial_number,
        "year": asset.year,
        "in_service_on": asset.in_service_on,
    }
    values.update(overrides)
    return values


# ---------- Servicio ----------


def test_quick_create_needs_only_code_name_and_class(in_a):
    cls = in_a.asset.asset_class
    add_class_attribute(
        membership=in_a.admin,
        asset_class=cls,
        label="Capacidad",
        data_type=AttributeType.NUMBER,
        required=True,
    )
    asset = create_asset(membership=in_a.admin, code=" BMB-01 ", name="Bomba", asset_class=cls)
    assert asset.code == "BMB-01"
    assert asset.status == Asset.Status.OPERATIONAL
    assert asset.criticality == Asset.Criticality.B
    assert asset.attributes == {}
    period = AssetStatusPeriod.objects.get(asset=asset)
    assert period.ended_at is None and period.status == Asset.Status.OPERATIONAL
    event = AuditEvent.objects.get(target_id=str(asset.pk), action=AuditEvent.Action.CREATE)
    assert event.changes["code"] == "BMB-01"


def test_full_create_stores_every_field(in_a):
    cls = in_a.asset.asset_class
    add_class_attribute(
        membership=in_a.admin, asset_class=cls, label="Potencia", data_type=AttributeType.NUMBER
    )
    site = Location.objects.create(code="P1", name="Planta 1")
    asset = create_asset(
        membership=in_a.admin,
        code="CMP-02",
        name="Compresor 2",
        asset_class=cls,
        location=site,
        parent=in_a.asset,
        criticality="A",
        manufacturer="Atlas",
        model="GA 30",
        serial_number="SN-1",
        year=2020,
        in_service_on=datetime.date(2021, 3, 1),
        attributes={"potencia": "30"},
    )
    asset.refresh_from_db()
    assert asset.location == site and asset.parent == in_a.asset
    assert (asset.manufacturer, asset.model, asset.serial_number, asset.year) == (
        "Atlas",
        "GA 30",
        "SN-1",
        2020,
    )
    assert asset.in_service_on == datetime.date(2021, 3, 1)
    assert asset.attributes == {"potencia": "30"}


def test_code_is_unique_in_the_organization_ignoring_case(in_a):
    with pytest.raises(ValidationError) as error:
        create_asset(membership=in_a.admin, code="cmp-01", name="x", asset_class=in_a.asset.asset_class)
    assert "code" in error.value.message_dict


def test_inactive_class_cannot_be_used_for_a_new_asset(in_a):
    cls = AssetClass.objects.create(code="OLD", name="Antigua", is_active=False)
    with pytest.raises(ValidationError) as error:
        create_asset(membership=in_a.admin, code="X-1", name="x", asset_class=cls)
    assert "asset_class" in error.value.message_dict


def test_invalid_year_is_rejected(in_a):
    with pytest.raises(ValidationError) as error:
        create_asset(
            membership=in_a.admin, code="X-1", name="x", asset_class=in_a.asset.asset_class, year=1800
        )
    assert "year" in error.value.message_dict


def test_update_changes_fields_and_audits_the_diff(in_a):
    asset = update_asset(
        membership=in_a.admin, asset=in_a.asset, **_fields(in_a.asset, name="Compresor principal")
    )
    asset.refresh_from_db()
    assert asset.name == "Compresor principal"
    event = AuditEvent.objects.get(target_id=str(asset.pk), action=AuditEvent.Action.UPDATE)
    assert event.changes["name"] == ["Compresor 1", "Compresor principal"]


def test_update_keeps_attributes_when_not_given_and_enforces_required_ones(in_a):
    cls = in_a.asset.asset_class
    add_class_attribute(
        membership=in_a.admin,
        asset_class=cls,
        label="Capacidad",
        data_type=AttributeType.NUMBER,
        required=True,
    )
    in_a.asset.attributes = {"capacidad": "5", "otro": "x"}
    in_a.asset.save()
    update_asset(membership=in_a.admin, asset=in_a.asset, **_fields(in_a.asset, attributes=None))
    assert in_a.asset.attributes == {"capacidad": "5", "otro": "x"}
    with pytest.raises(ValidationError):
        update_asset(membership=in_a.admin, asset=in_a.asset, **_fields(in_a.asset, attributes={}))


def test_update_does_not_change_status(in_a):
    """El estado cambia con `change_asset_status` (AST-04), no desde la ficha."""
    in_a.asset.status = Asset.Status.STOPPED
    in_a.asset.save()
    update_asset(membership=in_a.admin, asset=in_a.asset, **_fields(in_a.asset))
    in_a.asset.refresh_from_db()
    assert in_a.asset.status == Asset.Status.STOPPED


# ---------- Jerarquía sin ciclos (AST-03 CA1) ----------


def _chain(tenant):
    cls = tenant.asset.asset_class
    system = create_asset(
        membership=tenant.admin, code="SYS", name="Sistema", asset_class=cls, parent=tenant.asset
    )
    component = create_asset(
        membership=tenant.admin, code="CMP", name="Motor", asset_class=cls, parent=system
    )
    return tenant.asset, system, component


def test_asset_cannot_be_its_own_parent(in_a):
    with pytest.raises(ValidationError) as error:
        update_asset(membership=in_a.admin, asset=in_a.asset, **_fields(in_a.asset, parent=in_a.asset))
    assert "parent" in error.value.message_dict


def test_asset_cannot_hang_from_its_descendant(in_a):
    root, system, component = _chain(in_a)
    assert creates_cycle(asset=root, parent=component)
    assert not creates_cycle(asset=component, parent=root)
    with pytest.raises(ValidationError) as error:
        update_asset(membership=in_a.admin, asset=root, **_fields(root, parent=component))
    assert "parent" in error.value.message_dict
    root.refresh_from_db()
    assert root.parent is None


def test_asset_can_move_to_another_branch(in_a):
    root, system, component = _chain(in_a)
    update_asset(membership=in_a.admin, asset=component, **_fields(component, parent=root))
    component.refresh_from_db()
    assert component.parent == root


def test_retired_asset_cannot_be_a_new_parent(in_a):
    other = create_asset(
        membership=in_a.admin, code="OLD", name="Antiguo", asset_class=in_a.asset.asset_class
    )
    other.is_active = False
    other.save()
    with pytest.raises(ValidationError):
        create_asset(
            membership=in_a.admin, code="N", name="n", asset_class=in_a.asset.asset_class, parent=other
        )


# ---------- Vistas ----------


def test_create_view_quick_create(client, tenant_a):
    login(client, tenant_a.admin)
    url = reverse("asset_create")
    assert client.get(url).status_code == 200
    response = client.post(
        url,
        {"code": "NEW-1", "name": "Nuevo", "asset_class": tenant_a.asset.asset_class_id, "criticality": "B"},
    )
    with tenant_context(tenant_a.org):
        asset = Asset.objects.get(code="NEW-1")
    assert response.status_code == 302
    assert response["Location"] == reverse("asset_detail", args=[asset.pk])


def test_create_view_shows_service_errors_on_the_field(client, tenant_a):
    login(client, tenant_a.admin)
    response = client.post(
        reverse("asset_create"),
        {
            "code": "CMP-01",
            "name": "Duplicado",
            "asset_class": tenant_a.asset.asset_class_id,
            "criticality": "B",
        },
    )
    assert response.status_code == 200
    assert response.context["form"].errors["code"]


def test_create_view_prefills_parent(client, tenant_a):
    login(client, tenant_a.admin)
    response = client.get(reverse("asset_create"), {"parent": tenant_a.asset.pk})
    assert response.context["form"].initial["parent"] == tenant_a.asset.pk


def test_edit_view_saves_attributes_of_the_class(client, tenant_a):
    with tenant_context(tenant_a.org):
        add_class_attribute(
            membership=tenant_a.admin,
            asset_class=tenant_a.asset.asset_class,
            label="Combustible",
            data_type=AttributeType.CHOICE,
            choices=["Diésel", "Eléctrico"],
        )
    login(client, tenant_a.admin)
    url = reverse("asset_edit", args=[tenant_a.asset.pk])
    page = client.get(url)
    assert page.status_code == 200
    assert "attr_combustible" in page.content.decode()
    response = client.post(
        url,
        {
            "code": "CMP-01",
            "name": "Compresor 1",
            "asset_class": tenant_a.asset.asset_class_id,
            "criticality": "A",
            "in_service_on": "2022-05-10",
            "attr_combustible": "Diésel",
        },
    )
    assert response.status_code == 302
    # Fuera del contexto de la petición la fila solo se ve con bypass de RLS (ADR-026).
    with platform_context():
        tenant_a.asset.refresh_from_db()
    assert tenant_a.asset.attributes == {"combustible": "Diésel"}
    assert tenant_a.asset.in_service_on == datetime.date(2022, 5, 10)


def test_attribute_fields_fragment_follows_the_chosen_class(client, tenant_a):
    with tenant_context(tenant_a.org):
        other = AssetClass.objects.create(code="GRU", name="Grúa")
        add_class_attribute(
            membership=tenant_a.admin, asset_class=other, label="Tonelaje", data_type="number"
        )
    login(client, tenant_a.admin)
    body = client.get(reverse("asset_attribute_fields"), {"asset_class": other.pk}).content.decode()
    assert "attr_tonelaje" in body
    assert client.get(reverse("asset_attribute_fields"), {"asset_class": "no-uuid"}).status_code == 200


def test_detail_shows_components_failures_and_status_history(client, tenant_a):
    with tenant_context(tenant_a.org):
        root, system, _component = _chain(tenant_a)
        report_failure(
            membership=tenant_a.viewer, asset=root, description="Pierde presión", asset_stopped=True
        )
    login(client, tenant_a.admin)
    response = client.get(reverse("asset_detail", args=[root.pk]))
    body = response.content.decode()
    assert [c.pk for c in response.context["children"]] == [system.pk]
    assert "Pierde presión" in body
    assert len(response.context["failures"]) == 1
    assert len(response.context["status_periods"]) >= 1
    assert reverse("asset_edit", args=[root.pk]) in body


def test_asset_list_filters_by_parent(client, tenant_a):
    with tenant_context(tenant_a.org):
        root, system, _component = _chain(tenant_a)
    login(client, tenant_a.admin)
    response = client.get(reverse("asset_list"), {"parent": root.pk})
    assert [a.pk for a in response.context["page"].object_list] == [system.pk]


def test_service_rejects_membership_without_permission(in_a):
    with pytest.raises(PermissionDeniedError):
        create_asset(membership=in_a.tech, code="T", name="t", asset_class=in_a.asset.asset_class)
