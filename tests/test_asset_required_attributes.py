"""Datos técnicos obligatorios faltantes (AST-01): aviso en la ficha y filtro de la lista.

El alta rápida no exige los atributos obligatorios de la clase; la ficha avisa
cuántos faltan y la lista permite encontrar los activos incompletos.
"""

import pytest
from django.urls import reverse

from apps.accounts.models import MembershipLocation
from apps.assets.models import Asset, AssetClass, AssetClassAttribute
from apps.assets.selectors import asset_list
from apps.organizations.models import Location
from tests.conftest import login


def _attr(cls, key, *, required=True, is_active=True, data_type="text"):
    return AssetClassAttribute.objects.create(
        asset_class=cls,
        key=key,
        label=key.title(),
        data_type=data_type,
        required=required,
        is_active=is_active,
    )


@pytest.fixture
def catalog(in_a):
    """Clase CMP: `potencia` y `aceite` obligatorios, `color` opcional, `legado` obligatorio pero oculto.
    Clase BMB sin obligatorios."""
    cmp = in_a.asset.asset_class
    _attr(cmp, "potencia", data_type="number")
    _attr(cmp, "con-filtro", data_type="bool")
    _attr(cmp, "color", required=False)
    _attr(cmp, "legado", is_active=False)
    bmb = AssetClass.objects.create(code="BMB", name="Bomba")
    _attr(bmb, "caudal", required=False)

    def asset(code, cls=cmp, **attributes):
        return Asset.objects.create(code=code, name=code, asset_class=cls, attributes=attributes)

    in_a.asset.attributes = {"potencia": "7.5", "con-filtro": False}  # completo (False es un valor)
    in_a.asset.save()
    return {
        "complete": in_a.asset,
        "no_keys": asset("C-VACIO", color="rojo", legado=None),
        "empty_value": asset("C-BLANCO", potencia="", **{"con-filtro": True}),
        "null_value": asset("C-NULO", potencia=None, **{"con-filtro": True}),
        "one_missing": asset("C-UNO", potencia="3"),
        "other_class": asset("B-01", cls=bmb),
    }


def _incomplete_codes(membership):
    return set(asset_list(membership=membership, incomplete=True).values_list("code", flat=True))


def test_incomplete_filter_lists_assets_missing_required_values(in_a, catalog):
    assert _incomplete_codes(in_a.admin) == {"C-VACIO", "C-BLANCO", "C-NULO", "C-UNO"}
    # Sin el filtro, están todos.
    assert asset_list(membership=in_a.admin).count() == len(catalog)


def test_hidden_or_removed_required_attributes_do_not_count(in_a, catalog):
    AssetClassAttribute.objects.filter(key="con-filtro").update(is_active=False)
    AssetClassAttribute.objects.filter(key="potencia").delete()
    assert _incomplete_codes(in_a.admin) == set()


def test_filter_respects_site_scope(in_a, catalog):
    north = Location.objects.create(code="N", name="Planta Norte")
    catalog["one_missing"].location = north
    catalog["one_missing"].save()
    membership = in_a.admin
    membership.role = "planner"
    membership.restricted_to_locations = True
    membership.save()
    MembershipLocation.objects.create(membership=membership, location=north)
    assert _incomplete_codes(membership) == {"C-UNO"}


def test_list_view_filter(client, in_a, catalog):
    login(client, in_a.admin)
    response = client.get(reverse("asset_list"), {"incomplete": "1"})
    codes = {asset.code for asset in response.context["page"].object_list}
    assert codes == {"C-VACIO", "C-BLANCO", "C-NULO", "C-UNO"}
    assert response.context["incomplete"] is True
    assert "Con datos técnicos incompletos" in response.content.decode()
    # El modo etiquetas conserva el filtro en el formulario del PDF.
    html = client.get(reverse("asset_list"), {"incomplete": "1", "labels": "1"}).content.decode()
    assert 'name="incomplete" value="1"' in html


@pytest.mark.parametrize(
    ("key", "notice"),
    [
        ("no_keys", "Faltan 2 datos técnicos obligatorios."),
        ("one_missing", "Falta 1 dato técnico obligatorio."),
        ("complete", None),
    ],
)
def test_detail_notice_counts_missing_values(client, in_a, catalog, key, notice):
    login(client, in_a.admin)
    asset = catalog[key]
    html = client.get(reverse("asset_detail", args=[asset.pk])).content.decode()
    complete_link = f'<a href="{reverse("asset_attributes", args=[asset.pk])}">Completar</a>'
    if notice:
        assert notice in html and complete_link in html
    else:
        assert "obligatorio" not in html and complete_link not in html
