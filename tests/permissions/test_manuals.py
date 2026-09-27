"""Permisos de manuales (AST-06 CA3): suben y quitan quienes gestionan activos; leen quienes ven el activo."""

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from apps.accounts.models import MembershipLocation
from apps.assets.models import Asset, AssetClass
from apps.attachments.models import Attachment
from apps.attachments.selectors import search_manual_pages, visible_manuals
from apps.core.tenancy import tenant_context
from apps.organizations.models import Location
from tests.conftest import login, make_manual, pdf_bytes


@pytest.fixture(autouse=True)
def _media(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path


@pytest.mark.parametrize("role", ["tech", "viewer"])
def test_only_managers_upload_or_remove(client, tenant_a, role):
    with tenant_context(tenant_a.org):
        manual = make_manual(tenant_a.asset, ["x"], by=tenant_a.admin.user)
    login(client, getattr(tenant_a, role))
    upload = SimpleUploadedFile("m.pdf", pdf_bytes(["x"]), content_type="application/pdf")
    assert (
        client.post(reverse("asset_manual_add", args=[tenant_a.asset.pk]), {"file": upload}).status_code
        == 403
    )
    url = reverse("asset_class_manual_add", args=[tenant_a.asset.asset_class_id])
    assert client.post(url, {"file": upload}).status_code == 403
    assert client.post(reverse("manual_remove", args=[manual.pk])).status_code == 403
    # Pero leen y buscan.
    assert client.get(reverse("attachment", args=[manual.pk])).status_code == 200
    assert client.get(reverse("manual_search"), {"q": "x"}).status_code == 200
    html = client.get(reverse("asset_detail", args=[tenant_a.asset.pk])).content.decode()
    assert reverse("manual_remove", args=[manual.pk]) not in html
    with tenant_context(tenant_a.org):
        assert Attachment.objects.count() == 1


def test_search_requires_login(client, tenant_a):
    assert client.get(reverse("manual_search"), {"q": "x"}).status_code == 302


@pytest.fixture
def scoped(tenant_a):
    """El técnico queda limitado a Planta Norte; en Planta Sur hay otro activo de otra clase."""
    with tenant_context(tenant_a.org):
        north = Location.objects.create(code="N", name="Planta Norte")
        south = Location.objects.create(code="S", name="Planta Sur")
        tenant_a.asset.location = north
        tenant_a.asset.save()
        boiler_class = AssetClass.objects.create(code="CAL", name="Caldera")
        outside = Asset.objects.create(
            code="CAL-01", name="Caldera", asset_class=boiler_class, location=south
        )
        inside_manual = make_manual(tenant_a.asset, ["valvula norte"], by=tenant_a.admin.user)
        outside_manual = make_manual(outside, ["valvula sur"], by=tenant_a.admin.user)
        own_class = make_manual(tenant_a.asset.asset_class, ["valvula compresor"], by=tenant_a.admin.user)
        other_class = make_manual(boiler_class, ["valvula caldera"], by=tenant_a.admin.user)
        MembershipLocation.objects.create(membership=tenant_a.tech, location=north)
    tenant_a.tech.restricted_to_locations = True
    tenant_a.tech.save()
    return inside_manual, outside_manual, own_class, other_class


def test_site_scope_limits_manuals_and_search(tenant_a, scoped):
    inside_manual, outside_manual, own_class, other_class = scoped
    with tenant_context(tenant_a.org):
        visible = set(visible_manuals(membership=tenant_a.tech).values_list("pk", flat=True))
        assert visible == {inside_manual.pk, own_class.pk}
        hits = {page.attachment_id for page in search_manual_pages(membership=tenant_a.tech, query="valvula")}
        assert hits == {inside_manual.pk, own_class.pk}
        # Sin restricción se ve todo.
        assert visible_manuals(membership=tenant_a.viewer).count() == 4


def test_site_scope_blocks_download_of_hidden_manuals(client, tenant_a, scoped):
    inside_manual, outside_manual, own_class, other_class = scoped
    login(client, tenant_a.tech)
    assert client.get(reverse("attachment", args=[inside_manual.pk])).status_code == 200
    assert client.get(reverse("attachment", args=[own_class.pk])).status_code == 200
    assert client.get(reverse("attachment", args=[outside_manual.pk])).status_code == 404
    assert client.get(reverse("attachment", args=[other_class.pk])).status_code == 404
    html = client.get(reverse("manual_search"), {"q": "valvula"}).content.decode()
    assert "Caldera" not in html


def test_manuals_belong_to_the_assets_module(client, tenant_a):
    from tests.test_plan_limits import set_plan

    set_plan(tenant_a, features=["work_orders"])
    login(client, tenant_a.admin)
    assert client.get(reverse("manual_search"), {"q": "x"}).status_code == 403
    assert reverse("manual_search") not in client.get(reverse("menu")).content.decode()
