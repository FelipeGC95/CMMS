"""Ubicaciones en árbol (ORG-04) y atributos por clase de activo (AST-01)."""

import pytest
from django.core.exceptions import ValidationError
from django.urls import reverse

from apps.accounts.models import MembershipLocation
from apps.assets.models import Asset, AssetClassAttribute
from apps.assets.services import add_class_attribute, set_asset_attributes, update_class_attribute
from apps.audit.models import AuditEvent
from apps.core.tenancy import tenant_context
from apps.organizations.models import Location
from apps.organizations.selectors import location_tree
from apps.organizations.services import create_location, delete_location, update_location
from tests.conftest import login

# ---------- ORG-04 ----------


def _tree():
    plant = Location.objects.create(code="P1", name="Planta")
    area = Location.objects.create(code="A1", name="Envasado", kind="area", parent=plant)
    line = Location.objects.create(code="L1", name="Línea 1", kind="line", parent=area)
    return plant, area, line


def test_tree_lists_parents_before_children_with_path(in_a):
    plant, area, line = _tree()
    rows = location_tree(membership=in_a.admin)
    assert [r.location for r in rows] == [plant, area, line]
    assert [r.depth for r in rows] == [0, 1, 2]
    assert rows[2].path == "Planta › Envasado"


def test_create_location_is_audited_and_code_is_unique(in_a):
    location = create_location(membership=in_a.admin, code="P1", name="Planta", kind="site")
    assert AuditEvent.objects.filter(target_id=str(location.pk), action="create").exists()
    with pytest.raises(ValidationError):
        create_location(membership=in_a.admin, code="p1", name="Otra", kind="site")


def test_invalid_timezone_is_rejected(in_a):
    with pytest.raises(ValidationError):
        create_location(membership=in_a.admin, code="P1", name="Planta", kind="site", timezone="Marte/Base")


def test_location_cannot_move_inside_itself(in_a):
    plant, area, line = _tree()
    with pytest.raises(ValidationError):
        update_location(
            membership=in_a.admin, location=plant, code="P1", name="Planta", kind="site", parent=line
        )


def test_moving_a_node_carries_its_descendants(in_a):
    plant, area, line = _tree()
    other = Location.objects.create(code="P2", name="Planta 2")
    update_location(
        membership=in_a.admin, location=area, code="A1", name="Envasado", kind="area", parent=other
    )
    rows = location_tree(membership=in_a.admin)
    assert next(r for r in rows if r.location == line).path == "Planta 2 › Envasado"


def test_stored_path_follows_renames_and_moves(in_a):
    """La ruta guardada (str) se mantiene al renombrar o mover un nodo con su subárbol."""
    plant, area, line = _tree()
    assert str(line) == "Planta › Envasado › Línea 1"
    update_location(
        membership=in_a.admin, location=plant, code="P1", name="Planta Norte", kind="site", parent=None
    )
    line.refresh_from_db()
    assert str(line) == "Planta Norte › Envasado › Línea 1"
    other = Location.objects.create(code="P2", name="Planta 2")
    update_location(
        membership=in_a.admin, location=area, code="A1", name="Envasado", kind="area", parent=other
    )
    line.refresh_from_db()
    assert str(line) == "Planta 2 › Envasado › Línea 1"
    # La ruta no depende de cargar el padre: se lee sin consultas adicionales.
    fresh = Location.objects.get(pk=line.pk)
    assert not Location.parent.is_cached(fresh)
    assert str(fresh) == "Planta 2 › Envasado › Línea 1"


def test_delete_only_empty_locations(in_a):
    plant, area, line = _tree()
    with pytest.raises(ValidationError):
        delete_location(membership=in_a.admin, location=area)  # tiene hijos
    Asset.objects.filter(pk=in_a.asset.pk).update(location=line)
    with pytest.raises(ValidationError):
        delete_location(membership=in_a.admin, location=line)  # tiene activos
    empty = Location.objects.create(code="E", name="Vacía")
    delete_location(membership=in_a.admin, location=empty)
    assert not Location.objects.filter(pk=empty.pk).exists()


def test_location_screens(client, tenant_a):
    login(client, tenant_a.admin)
    response = client.post(reverse("location_list"), {"code": "P1", "name": "Planta", "kind": "site"})
    assert response.status_code == 302
    with tenant_context(tenant_a.org):
        plant = Location.objects.get(code="P1")
    response = client.post(
        reverse("location_edit", args=[plant.pk]),
        {"code": "P1", "name": "Planta Norte", "kind": "site", "is_active": "on"},
    )
    assert response.status_code == 302
    plant.refresh_from_db()
    assert plant.name == "Planta Norte"
    assert client.get(reverse("location_list")).status_code == 200


def test_scoped_manager_works_only_inside_its_subtree(in_a):
    plant, area, line = _tree()
    other = Location.objects.create(code="P2", name="Planta 2")
    planner = in_a.tech
    planner.role, planner.restricted_to_locations = "planner", True
    planner.save()
    MembershipLocation.objects.create(membership=planner, location=area)

    assert {r.location for r in location_tree(membership=planner)} == {area, line}
    create_location(membership=planner, code="L2", name="Línea 2", kind="line", parent=area)
    with pytest.raises(ValidationError):
        create_location(membership=planner, code="R", name="Raíz", kind="site")  # sin padre
    with pytest.raises(ValidationError):
        update_location(membership=planner, location=other, code="P2", name="x", kind="site", parent=None)


# ---------- AST-01 ----------


def test_attribute_key_is_fixed_and_unique(in_a):
    cls = in_a.asset.asset_class
    first = add_class_attribute(
        membership=in_a.admin, asset_class=cls, label="Potencia", data_type="number", unit="kW"
    )
    second = add_class_attribute(membership=in_a.admin, asset_class=cls, label="Potencia", data_type="text")
    assert (first.key, second.key) == ("potencia", "potencia_2")
    update_class_attribute(
        membership=in_a.admin,
        attribute=first,
        label="Potencia nominal",
        unit="kW",
        choices=[],
        required=False,
        order=1,
        is_active=True,
    )
    first.refresh_from_db()
    assert first.key == "potencia" and first.data_type == "number"


def test_list_attribute_needs_options(in_a):
    with pytest.raises(ValidationError):
        add_class_attribute(
            membership=in_a.admin, asset_class=in_a.asset.asset_class, label="Combustible", data_type="choice"
        )


def test_values_are_validated_stored_as_json_and_hidden_ones_kept(in_a):
    cls = in_a.asset.asset_class
    power = add_class_attribute(
        membership=in_a.admin, asset_class=cls, label="Potencia", data_type="number", unit="kW"
    )
    fuel = add_class_attribute(
        membership=in_a.admin,
        asset_class=cls,
        label="Combustible",
        data_type="choice",
        choices=["Diésel", "GLP"],
    )
    add_class_attribute(membership=in_a.admin, asset_class=cls, label="Inspección", data_type="date")
    add_class_attribute(membership=in_a.admin, asset_class=cls, label="Portátil", data_type="bool")
    asset = in_a.asset
    import datetime
    from decimal import Decimal

    set_asset_attributes(
        membership=in_a.admin,
        asset=asset,
        values={
            "potencia": Decimal("7.50"),
            "combustible": "GLP",
            "inspeccion": datetime.date(2026, 3, 1),
            "portatil": "true",
        },
    )
    asset.refresh_from_db()
    assert asset.attributes == {
        "potencia": "7.5",
        "combustible": "GLP",
        "inspeccion": "2026-03-01",
        "portatil": True,
    }
    assert AuditEvent.objects.filter(target_id=str(asset.pk), action="update").exists()

    with pytest.raises(ValidationError):
        set_asset_attributes(membership=in_a.admin, asset=asset, values={"combustible": "Leña"})

    # Ocultar un atributo no borra su valor (AST-01 CA3).
    fuel.is_active = False
    fuel.save()
    set_asset_attributes(membership=in_a.admin, asset=asset, values={"potencia": Decimal("8")})
    asset.refresh_from_db()
    assert asset.attributes["combustible"] == "GLP" and asset.attributes["potencia"] == "8"
    assert power.key in asset.attributes


def test_required_attribute(in_a):
    add_class_attribute(
        membership=in_a.admin,
        asset_class=in_a.asset.asset_class,
        label="Serie motor",
        data_type="text",
        required=True,
    )
    with pytest.raises(ValidationError):
        set_asset_attributes(membership=in_a.admin, asset=in_a.asset, values={})


def test_asset_page_shows_and_edits_attributes(client, tenant_a):
    with tenant_context(tenant_a.org):
        add_class_attribute(
            membership=tenant_a.admin,
            asset_class=tenant_a.asset.asset_class,
            label="Potencia",
            data_type="number",
            unit="kW",
        )
    login(client, tenant_a.admin)
    response = client.post(reverse("asset_attributes", args=[tenant_a.asset.pk]), {"attr_potencia": "7,5"})
    assert response.status_code == 302, response.content.decode()[:500]
    page = client.get(reverse("asset_detail", args=[tenant_a.asset.pk])).content.decode()
    assert "Potencia" in page and "kW" in page


def test_class_screens(client, tenant_a):
    login(client, tenant_a.admin)
    response = client.post(
        reverse("asset_class_list"),
        {"code": "GRH", "name": "Grúa horquilla", "iso_category": "vehicle", "level_hint": "equipment"},
    )
    assert response.status_code == 302
    detail = response["Location"]
    response = client.post(
        detail, {"action": "attribute", "label": "Capacidad", "data_type": "number", "unit": "kg"}
    )
    assert response.status_code == 302
    with tenant_context(tenant_a.org):
        attribute = AssetClassAttribute.objects.get(key="capacidad")
    response = client.post(
        reverse("attribute_edit", args=[attribute.asset_class_id, attribute.pk]),
        {"label": "Capacidad", "unit": "t", "order": "1"},
    )
    assert response.status_code == 302
    attribute.refresh_from_db()
    assert attribute.unit == "t" and not attribute.is_active  # desmarcado = oculto
    assert client.get(reverse("asset_class_list")).status_code == 200
