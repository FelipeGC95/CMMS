"""Comparación de planes en Facturación (ORG-13): qué trae cada plan sale de `Plan.features` y sus cupos."""

import re

import pytest
from django.urls import reverse

from apps.billing.selectors import billing_overview
from apps.billing.templatetags.billing_tags import storage
from apps.core.tenancy import tenant_context
from apps.organizations.models import Plan
from apps.organizations.modules import MODULE_DESCRIPTIONS, MODULE_LABELS, plan_features, plan_includes
from tests.billing_utils import authorize, install_gateway, make_billable, plans, store_uf
from tests.conftest import login


@pytest.fixture
def billable(tenant_a):
    plans()
    make_billable(tenant_a.org)
    tenant_a.org.refresh_from_db()
    tenant_a.admin.organization = tenant_a.org
    with tenant_context(tenant_a.org):
        store_uf()
    return tenant_a


def _overview(tenant):
    with tenant_context(tenant.org):
        return billing_overview(membership=tenant.admin)


def _column(overview, code):
    return next(column for column in overview.columns if column.plan.code == code)


def _keys(features):
    return [feature.key for feature in features]


def test_every_module_has_a_customer_description_and_a_row():
    assert set(MODULE_DESCRIPTIONS) == set(MODULE_LABELS)
    rows = plan_features()
    modules = {row.module for row in rows if row.module}
    assert modules == set(MODULE_LABELS)
    assert len({row.key for row in rows}) == len(rows)
    # Derivadas: los indicadores básicos van en todos los planes y el modo sin conexión va con las OT.
    by_key = {row.key: row for row in rows}
    assert by_key["basic_kpis"].module is None
    assert by_key["offline"].module == "work_orders"


def test_empty_features_means_everything_is_included():
    plan = Plan(code="x", name="X", features=[])
    assert all(plan_includes(plan, row.module) for row in plan_features())
    assert plan_includes(Plan(code="y", name="Y", features=["assets"]), None)
    assert not plan_includes(Plan(code="y", name="Y", features=["assets"]), "inventory")


def test_columns_follow_plan_features_in_the_same_order(billable):
    overview = _overview(billable)
    assert [column.plan.code for column in overview.columns] == ["trial", "basic", "pro", "enterprise"]
    order = _keys(plan_features())
    for column in overview.columns:
        assert [cell.feature.key for cell in column.cells] == order
    basic, pro, enterprise = (_column(overview, code) for code in ("basic", "pro", "enterprise"))
    assert set(_keys(basic.excluded)) == {
        "inventory",
        "condition",
        "assistant",
        "api",
        "exports",
        "custom_roles",
        "sso",
    }
    assert {"assets", "work_orders", "offline", "preventive", "requests", "basic_kpis"} <= set(
        _keys(basic.included)
    )
    assert _keys(pro.excluded) == ["sso"]
    assert enterprise.excluded == []
    assert enterprise.kind == "quoted"
    # La tabla de escritorio recorre las mismas celdas.
    feature, cells = overview.feature_rows[_keys(plan_features()).index("sso")]
    assert [included for _, included in cells] == [False, False, False, True]


def test_trial_is_current_and_downgrades_list_lost_modules(billable):
    overview = _overview(billable)
    trial = _column(overview, "trial")
    assert trial.is_current and trial.kind == "trial"
    assert [column.plan.code for column in overview.columns if column.is_current] == ["trial"]
    assert _column(overview, "basic").lost == ["Inventario y repuestos", "Mantenimiento por condición"]
    assert _column(overview, "pro").lost == []


def test_plan_with_empty_features_shows_everything_included(billable):
    Plan.objects.filter(code="basic").update(features=[])
    basic = _column(_overview(billable), "basic")
    assert basic.excluded == []
    assert len(basic.included) == len(plan_features())


def test_page_shows_quotas_included_and_excluded(client, billable):
    login(client, billable.admin)
    body = client.get(reverse("billing")).content.decode()
    assert "Qué incluye cada plan" in body
    assert "300 incluidos; +1 UF/mes cada 100 más" in body  # tramo de Profesional
    assert "Hasta 50" in body and "Hasta 10" in body  # Básico
    assert "Ilimitados" in body  # usuarios de Profesional (cupo en blanco)
    assert "2 GB" in body and "20 GB" in body and "Ilimitado" in body
    assert "100 al mes" in body and "500 al mes" in body
    assert "Modo sin conexión en terreno" in body
    assert "No incluye (" in body  # plegable de móvil
    assert "Con este plan dejas de tener: Inventario y repuestos, Mantenimiento por condición." in body
    assert "Tu prueba" in body
    # Toda celda de la tabla lleva data-label.
    table = body[body.index('class="table plan-compare__table"') :]
    table = table[: table.index("</table>")]
    assert "<td>" not in table and "<td " in table
    assert all("data-label=" in td for td in re.findall(r"<td[^>]*>", table))


def test_subscribed_plan_is_marked_as_current(billable, monkeypatch, settings, client):
    gateway = install_gateway(monkeypatch, settings)
    from apps.billing import services

    with tenant_context(billable.org):
        url = services.start_checkout(
            membership=billable.admin, plan=Plan.objects.get(code="pro"), period="monthly"
        )
    authorize(gateway, url.rsplit("/", 1)[1])
    billable.org.refresh_from_db()
    overview = _overview(billable)
    assert [column.plan.code for column in overview.columns if column.is_current] == ["pro"]
    assert "trial" not in [column.plan.code for column in overview.columns]
    assert "Inventario y repuestos" in _column(overview, "basic").lost
    assert "API" in _column(overview, "basic").lost
    login(client, billable.admin)
    body = client.get(reverse("billing")).content.decode()
    assert "Tu plan" in body and "Cambiar a Básico" in body


def test_storage_format():
    assert storage(2048) == "2 GB"
    assert storage(1536) == "1,5 GB"
    assert storage(500) == "500 MB"
    assert storage(None) == ""


def test_contact_email_only_when_configured(client, billable, settings):
    login(client, billable.admin)
    settings.BILLING_CONTACT_EMAIL = ""
    body = client.get(reverse("billing")).content.decode()
    assert "mailto:" not in body and "Cotizar" not in body
    assert "Plan con precio a convenir." in body
    assert "El pago en línea aún no está activo." in body
    settings.BILLING_CONTACT_EMAIL = "contacto@galbust.com"
    body = client.get(reverse("billing")).content.decode()
    assert "mailto:contacto@galbust.com" in body and "escríbenos a contacto@galbust.com" in body
