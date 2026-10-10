"""Enlaces "Modo sin conexión" a /field/ (X-06, ADR-011): solo para quien ejecuta OT y tiene el módulo."""

import pytest
from django.urls import reverse

from apps.organizations.models import Plan
from tests.conftest import login

FIELD_LINK = 'href="/field/" data-field-link'


def pages():
    return {
        "menu": reverse("menu"),
        "mine": reverse("work_order_list") + "?scope=mine",
        "week": reverse("work_order_my_week"),
    }


@pytest.mark.parametrize("role", ["admin", "tech"])
def test_executors_see_the_offline_link_in_my_work_orders_my_week_and_more(client, tenant_a, role):
    login(client, getattr(tenant_a, role))
    for name, url in pages().items():
        response = client.get(url)
        assert response.status_code == 200, name
        html = response.content.decode()
        assert FIELD_LINK in html, name
        assert "Modo sin conexión" in html, name


def test_the_link_is_only_in_the_mine_scope_of_the_list(client, tenant_a):
    login(client, tenant_a.admin)
    html = client.get(reverse("work_order_list") + "?scope=open").content.decode()
    assert FIELD_LINK not in html


def test_viewer_does_not_see_the_offline_link(client, tenant_a):
    login(client, tenant_a.viewer)
    assert FIELD_LINK not in client.get(reverse("menu")).content.decode()
    assert FIELD_LINK not in client.get(reverse("work_order_list") + "?scope=mine").content.decode()
    assert client.get(reverse("work_order_my_week")).status_code == 403


def test_plan_without_work_orders_hides_the_offline_link(client, tenant_a):
    tenant_a.org.plan = Plan.objects.create(code="solo-activos", name="Solo activos", features=["assets"])
    tenant_a.org.save(update_fields=["plan"])
    login(client, tenant_a.tech)
    assert FIELD_LINK not in client.get(reverse("menu")).content.decode()


@pytest.mark.parametrize("role", ["admin", "tech", "viewer"])
def test_every_logout_and_org_switch_form_wipes_field_data(client, tenant_a, role):
    """Los formularios de salida y de cambio de organización los intercepta static/js/app.js."""
    login(client, getattr(tenant_a, role))
    for url in (reverse("dashboard"), reverse("menu")):
        html = client.get(url).content.decode()
        assert html.count(f'action="{reverse("account_logout")}" data-field-exit') >= 1, url
        assert 'id="field-exit-dialog"' in html, url
