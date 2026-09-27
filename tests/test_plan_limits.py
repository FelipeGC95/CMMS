"""ORG-10: cupo de activos (aviso al 80 %, bloqueo al 100 %) y módulos por plan."""

import re

import pytest
from django.urls import reverse

from apps.assets.imports import apply_import
from apps.assets.models import Asset
from apps.assets.services import create_asset, update_asset
from apps.core.tenancy import tenant_context
from apps.organizations.models import Plan
from apps.organizations.modules import has_module, module_for_path
from apps.organizations.plans import BASE_MODULES
from apps.organizations.services import QuotaExceeded, check_quota, quota_warning
from tests.api_utils import api_client
from tests.conftest import login


def set_plan(tenant, *, max_assets=None, features=None, code="test"):
    plan = Plan.objects.create(
        code=code,
        name="Prueba corta",
        max_assets=max_assets,
        features=BASE_MODULES if features is None else features,
    )
    tenant.org.plan = plan
    tenant.org.save(update_fields=["plan"])
    return plan


def new_asset(tenant, code):
    with tenant_context(tenant.org):
        return create_asset(
            membership=tenant.admin, code=code, name=code, asset_class=tenant.asset.asset_class
        )


# ---------- Cupo de activos ----------


def test_creation_blocked_at_100_percent_but_editing_allowed(tenant_a):
    set_plan(tenant_a, max_assets=2)  # ya tiene 1 activo
    new_asset(tenant_a, "A-2")
    with pytest.raises(QuotaExceeded):
        new_asset(tenant_a, "A-3")
    with tenant_context(tenant_a.org):
        assert Asset.objects.count() == 2
        update_asset(
            membership=tenant_a.admin,
            asset=tenant_a.asset,
            code=tenant_a.asset.code,
            name="Renombrado",
            asset_class=tenant_a.asset.asset_class,
        )
        tenant_a.asset.refresh_from_db()
        assert tenant_a.asset.name == "Renombrado"


def test_warning_from_80_percent(tenant_a):
    set_plan(tenant_a, max_assets=5)
    with tenant_context(tenant_a.org):
        assert quota_warning(organization=tenant_a.org, resource="assets") is None
    for i in range(3):
        new_asset(tenant_a, f"W-{i}")
    with tenant_context(tenant_a.org):
        warning = quota_warning(organization=tenant_a.org, resource="assets")
    assert "4 de 5" in warning and "80" in warning


def test_unlimited_plan_and_no_plan_never_block(tenant_a):
    with tenant_context(tenant_a.org):
        check_quota(organization=tenant_a.org, resource="assets", adding=10_000)
    set_plan(tenant_a, max_assets=None)
    with tenant_context(tenant_a.org):
        check_quota(organization=tenant_a.org, resource="assets", adding=10_000)


def test_create_page_shows_warning_and_blocks(client, tenant_a):
    set_plan(tenant_a, max_assets=1)
    login(client, tenant_a.admin)
    page = client.get(reverse("asset_create"))
    assert "1 de 1" in page.content.decode()
    data = {"code": "X-9", "name": "Nuevo", "asset_class": tenant_a.asset.asset_class_id, "criticality": "B"}
    response = client.post(reverse("asset_create"), data)
    assert response.status_code == 200 and "cambiar de plan" in response.content.decode()


def test_excel_import_is_all_or_nothing_against_quota(tenant_a):
    set_plan(tenant_a, max_assets=2)
    rows = [{"data": {"code": f"I-{i}", "name": "Importado"}} for i in range(2)]
    session = {"rows": rows, "new_classes": [], "new_locations": []}
    with tenant_context(tenant_a.org):
        with pytest.raises(QuotaExceeded):
            apply_import(session)
        assert Asset.objects.count() == 1
        # Actualizar un activo existente no consume cupo.
        apply_import({**session, "rows": [{"data": {"code": tenant_a.asset.code, "name": "Otro"}}]})


def test_api_creation_blocked_by_quota(tenant_a):
    set_plan(tenant_a, max_assets=1, features=[*BASE_MODULES, "api"])
    client = api_client(tenant_a)
    response = client.post(
        "/api/v1/assets/",
        {"code": "API-1", "name": "Por API", "asset_class": str(tenant_a.asset.asset_class_id)},
        format="json",
    )
    assert response.status_code == 400
    assert "cambiar de plan" in str(response.json()["error"]["details"])
    assert client.get("/api/v1/assets/").status_code == 200


# ---------- Módulos ----------


@pytest.mark.parametrize(
    "path,module",
    [
        ("/assets/export.xlsx", "exports"),
        ("/work-orders/0190c0de-0000-7000-8000-000000000000/sheet.pdf", "exports"),
        ("/settings/api-tokens/", "api"),
        ("/plans/", "preventive"),
        ("/requests/", "requests"),
        ("/report/", "requests"),
        ("/work-orders/", "work_orders"),
        ("/failure-codes/", "work_orders"),
        ("/assets/", "assets"),
        ("/q/abc/", "assets"),
        ("/", None),
        ("/settings/", None),
        ("/locations/", None),
    ],
)
def test_module_map(path, module):
    assert module_for_path(path) == module


def test_module_not_in_plan_answers_403_and_is_hidden(client, tenant_a):
    set_plan(tenant_a, features=["assets", "work_orders", "requests", "kpis"])  # sin preventive/api/exports
    login(client, tenant_a.admin)
    for url in (reverse("plan_list"), reverse("asset_export_xlsx"), reverse("api_token_list")):
        response = client.get(url)
        assert response.status_code == 403
        assert "no está incluido en tu plan" in response.content.decode()
    menu = client.get(reverse("menu")).content.decode()
    assert reverse("plan_list") not in menu and reverse("api_token_list") not in menu
    assert reverse("asset_list") in menu
    assert client.get(reverse("asset_list")).status_code == 200


def test_work_orders_hidden_from_navigation(client, tenant_a):
    set_plan(tenant_a, features=["assets"])
    login(client, tenant_a.admin)
    page = client.get(reverse("dashboard")).content.decode()
    navs = "".join(re.findall(r'<nav class="(?:navbar__links|bottom-nav)".*?</nav>', page, re.S))
    assert navs and reverse("work_order_list") not in navs
    assert client.get(reverse("work_order_list")).status_code == 403


def test_api_needs_api_module(tenant_a):
    set_plan(tenant_a, features=BASE_MODULES)
    response = api_client(tenant_a).get("/api/v1/assets/")
    assert response.status_code == 403
    assert "no está incluido en tu plan" in response.json()["error"]["message"]


def test_api_endpoint_module_is_checked(tenant_a):
    set_plan(tenant_a, features=["assets", "api"])
    client = api_client(tenant_a)
    assert client.get("/api/v1/assets/").status_code == 200
    assert client.get("/api/v1/work-orders/").status_code == 403


def test_default_plans_include_base_modules(db):
    trial = Plan.objects.get(code="trial")
    assert all(has_module(type("O", (), {"plan": trial})(), m) for m in BASE_MODULES)
    assert not has_module(type("O", (), {"plan": trial})(), "api")
