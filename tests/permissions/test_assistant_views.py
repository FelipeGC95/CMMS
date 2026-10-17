"""Permisos y módulo de las pantallas del asistente (AI-05, ADR-013, ORG-10)."""

import uuid

import pytest
from django.urls import reverse

from apps.accounts.models import Membership
from apps.assistant import services
from apps.assistant.models import AssistantConversation
from apps.core.tenancy import tenant_context
from apps.organizations.models import Plan
from tests.assistant_utils import install
from tests.conftest import login
from tests.inventory_utils import add_member

Role = Membership.Role
# rol: (pide ayuda, ve la lista)
MATRIX = {
    Role.ADMIN: (True, True),
    Role.PLANNER: (True, True),
    Role.SUPERVISOR: (True, True),
    Role.TECHNICIAN: (True, True),
    Role.VIEWER: (False, False),
    Role.EXTERNAL: (False, False),
}


@pytest.fixture(autouse=True)
def configured(settings, monkeypatch):
    settings.ANTHROPIC_API_KEY = "test-key"
    settings.AI_ASSISTANT_USE_QUEUE = False
    install(monkeypatch)


def _member(tenant, role):
    return {Role.ADMIN: tenant.admin, Role.TECHNICIAN: tenant.tech, Role.VIEWER: tenant.viewer}.get(
        role
    ) or add_member(tenant, role)


@pytest.mark.parametrize("role", list(MATRIX))
def test_screens_follow_the_role_matrix(client, tenant_a, role):
    use, read = MATRIX[role]
    membership = _member(tenant_a, role)
    login(client, membership)
    start = f"{reverse('assistant_start')}?asset={tenant_a.asset.pk}"
    assert client.get(start).status_code == (200 if use else 403)
    response = client.post(start, {"text": "Ruido", "client_uuid": uuid.uuid4()})
    assert response.status_code == (302 if use else 403)
    assert client.get(reverse("assistant_list")).status_code == (200 if read else 403)
    detail = client.get(reverse("asset_detail", args=[tenant_a.asset.pk]))
    if detail.status_code == 200:
        assert (reverse("assistant_start") in detail.text) is use
    menu = client.get(reverse("menu"))
    if menu.status_code == 200:
        assert (reverse("assistant_list") in menu.text) is read
    with tenant_context(tenant_a.org):
        assert AssistantConversation.objects.count() == (1 if use else 0)


def test_only_the_author_acts_on_a_conversation(client, tenant_a):
    with tenant_context(tenant_a.org):
        conversation = services.start_conversation(
            membership=tenant_a.tech, asset=tenant_a.asset, text="Ruido"
        )
    supervisor = add_member(tenant_a, Role.SUPERVISOR)
    login(client, supervisor)
    # Sin `view_all`, otro usuario ni siquiera la ve.
    assert client.get(reverse("assistant_conversation", args=[conversation.pk])).status_code == 404
    assert client.post(reverse("assistant_close", args=[conversation.pk])).status_code == 404
    login(client, tenant_a.viewer)
    assert client.get(reverse("assistant_conversation", args=[conversation.pk])).status_code == 403
    assert (
        client.post(reverse("assistant_helpful", args=[conversation.pk]), {"helpful": "yes"}).status_code
        == 403
    )


@pytest.mark.parametrize("code", ["trial", "basic"])
def test_module_off_answers_403_and_hides_the_entry_points(client, tenant_a, code):
    with tenant_context(tenant_a.org):
        conversation = services.start_conversation(
            membership=tenant_a.tech, asset=tenant_a.asset, text="Ruido"
        )
    tenant_a.org.plan = Plan.objects.get(code=code)
    tenant_a.org.save(update_fields=["plan"])
    login(client, tenant_a.tech)
    for url in (
        reverse("assistant_list"),
        f"{reverse('assistant_start')}?asset={tenant_a.asset.pk}",
        reverse("assistant_conversation", args=[conversation.pk]),
        reverse("assistant_thread", args=[conversation.pk]),
    ):
        assert client.get(url).status_code == 403, url
    assert client.post(reverse("assistant_close", args=[conversation.pk])).status_code == 403
    assert reverse("assistant_list") not in client.get(reverse("menu")).text
