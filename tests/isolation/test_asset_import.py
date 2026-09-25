"""Aislamiento de la importación de activos entre organizaciones (NF-07)."""

from django.urls import reverse

from apps.accounts.models import Membership
from apps.assets.imports import apply_import, build_preview, build_template, parse_workbook
from apps.assets.models import Asset, AssetClass
from apps.core.tenancy import tenant_context
from tests.conftest import login
from tests.test_asset_import import row, xlsx


def test_same_code_in_other_org_creates_instead_of_updating(tenant_a, tenant_b):
    # Ambas tienen CMP-01 y la clase "Compresor"; importar en B jamás toca A.
    with tenant_context(tenant_b.org):
        preview = build_preview(parse_workbook(xlsx([row("CMP-01", "Renombrado en B"), row("B-2")])))
        assert [r.action for r in preview.rows] == ["update", "create"]
        apply_import(preview.to_session())
        assert AssetClass.objects.count() == 1  # reutiliza la clase de B, no la de A
    with tenant_context(tenant_a.org):
        assert Asset.objects.get(code="CMP-01").name == "Compresor 1"
        assert not Asset.objects.filter(code="B-2").exists()


def test_template_only_lists_own_classes_and_locations(tenant_a, tenant_b):
    with tenant_context(tenant_a.org):
        AssetClass.objects.create(code="SECRETA", name="Clase secreta de A")
    with tenant_context(tenant_b.org):
        content = build_template()
    assert b"Clase secreta de A" not in content


def test_preview_token_cannot_be_confirmed_in_another_organization(client, tenant_a, tenant_b):
    Membership.objects.create(user=tenant_a.admin.user, organization=tenant_b.org, role="admin")
    login(client, tenant_a.admin)
    client.post(reverse("switch_organization"), {"organization": str(tenant_a.org.id)})
    response = client.post(reverse("asset_import"), {"file": xlsx([row("CROSS-1")])})
    token = response.context["token"]

    client.post(reverse("switch_organization"), {"organization": str(tenant_b.org.id)})
    client.post(reverse("asset_import_confirm", args=[token]))
    for tenant in (tenant_a, tenant_b):
        with tenant_context(tenant.org):
            assert not Asset.objects.filter(code="CROSS-1").exists()
