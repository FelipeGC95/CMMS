"""Solo lectura en las pantallas secundarias (ORG-13 CA5, P-03): ficha de ubicación y de clase,
borrador de plan, equipo, roles y SSO no muestran acciones de escritura; los datos se siguen
viendo (formularios deshabilitados). La PWA de terreno recibe los permisos de escritura en falso
y oculta sus botones. Una organización al día ve todo como siempre."""

import datetime

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.accounts.services import create_custom_role, invite_member
from apps.core.tenancy import tenant_context
from apps.maintenance.services import create_draft
from apps.organizations.models import Location, Organization, Plan
from apps.sso.models import SsoConfiguration, SsoDomain
from tests.api_utils import FIELD_API, field_client
from tests.billing_utils import make_billable
from tests.conftest import login

pytestmark = pytest.mark.django_db


@pytest.fixture(params=["read_only", "active"])
def secondary(request, tenant_a):
    enterprise = Plan.objects.get(code="enterprise")
    if request.param == "read_only":
        make_billable(tenant_a.org)
        Organization.objects.filter(pk=tenant_a.org.pk).update(
            trial_ends_at=timezone.now() - datetime.timedelta(days=30), plan=enterprise
        )
    else:
        Organization.objects.filter(pk=tenant_a.org.pk).update(plan=enterprise)
    tenant_a.org.refresh_from_db()
    with tenant_context(tenant_a.org):
        tenant_a.location = Location.objects.create(code="PLT", name="Planta")
        tenant_a.draft = create_draft(membership=tenant_a.admin, plan=tenant_a.plan)
        tenant_a.item = tenant_a.draft.items.first()
        tenant_a.role = create_custom_role(
            membership=tenant_a.admin, name="Lubricador", base_role="technician"
        )
        tenant_a.invitation = invite_member(
            membership=tenant_a.admin, email="nueva@empresa-a.cl", role="viewer", base_url="http://testserver"
        )
        SsoConfiguration.objects.create(
            discovery_url="https://idp.example.com/.well-known/openid-configuration", client_id="app"
        )
        tenant_a.domain = SsoDomain.objects.create(domain="empresa-a.cl", verification_token="tok")
    return request.param, tenant_a


def _pages(t):
    """(URL, marcas de escritura que se ocultan en solo lectura)."""
    asset_class = t.asset.asset_class
    plan = t.plan
    item = t.item
    return [
        (
            reverse("location_edit", args=[t.location.pk]),
            ['type="submit"', reverse("location_delete", args=[t.location.pk])],
        ),
        (
            reverse("asset_class_detail", args=[asset_class.pk]),
            ['value="attribute"', 'value="class"', reverse("asset_class_manual_add", args=[asset_class.pk])],
        ),
        (
            reverse("plan_draft", args=[plan.pk]),
            [
                reverse("plan_publish", args=[plan.pk]),
                reverse("plan_discard", args=[plan.pk]),
                reverse("plan_item_edit", args=[plan.pk, item.pk]),
                reverse("plan_item_remove", args=[plan.pk, item.pk]),
                reverse("plan_item_move", args=[plan.pk, item.pk]),
                'value="item"',
                'value="resource"',
                'type="submit"',
            ],
        ),
        (reverse("team"), [reverse("invitation_revoke", args=[t.invitation.pk]), 'name="email"']),
        (
            reverse("member_edit", args=[t.tech.pk]),
            [reverse("member_active", args=[t.tech.pk]), 'type="submit"'],
        ),
        (reverse("custom_role_list"), ['name="base_role"']),
        (
            reverse("custom_role_edit", args=[t.role.pk]),
            [reverse("custom_role_active", args=[t.role.pk]), 'type="submit"'],
        ),
        (
            reverse("sso_settings"),
            [
                reverse("sso_test"),
                reverse("sso_domain_add"),
                reverse("sso_domain_verify", args=[t.domain.pk]),
                reverse("sso_domain_remove", args=[t.domain.pk]),
                'type="submit"',
            ],
        ),
    ]


def test_secondary_screens_hide_write_actions(client, secondary):
    state, tenant = secondary
    login(client, tenant.admin)
    for url, writes in _pages(tenant):
        response = client.get(url)
        assert response.status_code == 200, url
        html = response.content.decode()
        # El formulario de cuenta (salir) va en la cabecera: se cuentan solo los del contenido.
        main = html.split('id="main"', 1)[-1].split("</main>", 1)[0]
        for write in writes:
            if state == "read_only":
                assert write not in main, (url, write)
            else:
                assert write in main, (url, write)
        if state == "read_only" and 'class="form-lock"' in main:
            assert 'class="form-lock" disabled' in main, url


def test_field_bootstrap_denies_write_permissions_in_read_only(secondary):
    state, tenant = secondary
    permissions = field_client(tenant.tech).get(f"{FIELD_API}/bootstrap/").json()["permissions"]
    expected = state != "read_only"
    assert permissions == {"execute": expected, "request_create": expected, "reading_create": expected}
