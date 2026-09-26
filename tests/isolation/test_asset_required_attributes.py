"""Aislamiento de "datos técnicos incompletos" (AST-01): las clases y atributos de B no
cambian qué activos de A están incompletos, y el filtro de B nunca lista activos de A."""

from django.urls import reverse

from apps.assets.models import AssetClassAttribute
from apps.assets.selectors import asset_list
from apps.core.tenancy import tenant_context
from tests.conftest import login


def test_other_org_attributes_do_not_affect_filter_or_notice(client, tenant_a, tenant_b):
    # Misma clave y mismo código de clase ("CMP") en B, obligatorio: A no se ve afectada.
    with tenant_context(tenant_b.org):
        AssetClassAttribute.objects.create(
            asset_class=tenant_b.asset.asset_class,
            key="potencia",
            label="Potencia",
            data_type="text",
            required=True,
        )
    with tenant_context(tenant_a.org):
        assert not asset_list(membership=tenant_a.admin, incomplete=True).exists()
    with tenant_context(tenant_b.org):
        assert list(asset_list(membership=tenant_b.admin, incomplete=True)) == [tenant_b.asset]

    login(client, tenant_a.admin)
    response = client.get(reverse("asset_detail", args=[tenant_a.asset.pk]))
    assert response.context["missing_required_count"] == 0
    listed = client.get(reverse("asset_list"), {"incomplete": "1"}).context["page"].object_list
    assert list(listed) == []

    login(client, tenant_b.admin)
    listed = client.get(reverse("asset_list"), {"incomplete": "1"}).context["page"].object_list
    assert [asset.pk for asset in listed] == [tenant_b.asset.pk]
