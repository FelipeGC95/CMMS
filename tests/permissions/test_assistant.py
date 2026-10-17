"""Permisos, módulo y cupo por plan del asistente de diagnóstico (AI-05, ADR-013, ORG-10)."""

import pytest

from apps.accounts.models import Membership
from apps.accounts.permissions import Perm, PermissionDeniedError, has_perm
from apps.assistant import selectors, services
from apps.core.tenancy import tenant_context
from apps.organizations.models import Plan
from apps.organizations.modules import ModuleNotIncludedError, has_module, module_for_path
from apps.organizations.plans import ASSISTANT_CONVERSATION_QUOTAS, DEFAULT_PLANS
from tests.assistant_utils import install
from tests.inventory_utils import add_member

Role = Membership.Role

# rol: (assistant.use, assistant.view_all)
MATRIX = {
    Role.ADMIN: (True, True),
    Role.PLANNER: (True, False),
    Role.SUPERVISOR: (True, False),
    Role.TECHNICIAN: (True, False),
    Role.VIEWER: (False, False),
    Role.EXTERNAL: (False, False),
}


@pytest.fixture(autouse=True)
def configured(settings, monkeypatch):
    settings.ANTHROPIC_API_KEY = "test-key"
    settings.AI_ASSISTANT_USE_QUEUE = False
    install(monkeypatch)


@pytest.mark.parametrize("role", list(MATRIX))
def test_role_matrix(role):
    use, view_all = MATRIX[role]
    membership = Membership(role=role)
    assert has_perm(membership, Perm.ASSISTANT_USE) is use
    assert has_perm(membership, Perm.ASSISTANT_VIEW_ALL) is view_all


@pytest.fixture
def members(tenant_a):
    return {
        Role.ADMIN: tenant_a.admin,
        Role.PLANNER: add_member(tenant_a, Role.PLANNER),
        Role.SUPERVISOR: add_member(tenant_a, Role.SUPERVISOR),
        Role.TECHNICIAN: tenant_a.tech,
        Role.VIEWER: tenant_a.viewer,
        Role.EXTERNAL: add_member(tenant_a, Role.EXTERNAL),
    }


@pytest.mark.parametrize("role", list(MATRIX))
def test_services_and_selectors_enforce_permissions(tenant_a, members, role):
    use, view_all = MATRIX[role]
    membership = members[role]
    with tenant_context(tenant_a.org):
        theirs = services.start_conversation(membership=tenant_a.tech, asset=tenant_a.asset, text="Ruido")
        start = lambda: services.start_conversation(  # noqa: E731
            membership=membership, asset=tenant_a.asset, text="Ruido"
        )
        if use:
            own = start()
            listed = set(selectors.conversation_list(membership=membership))
            assert own in listed
            assert (theirs in listed) is (view_all or role == Role.TECHNICIAN)
        else:
            with pytest.raises(PermissionDeniedError):
                start()
            with pytest.raises(PermissionDeniedError):
                selectors.conversation_list(membership=membership)


def test_admin_reads_but_does_not_act_on_others_conversations(tenant_a):
    with tenant_context(tenant_a.org):
        conversation = services.start_conversation(
            membership=tenant_a.tech, asset=tenant_a.asset, text="Ruido"
        )
        assert selectors.conversation_for(membership=tenant_a.admin, pk=conversation.pk) == conversation
        for attempt in (
            lambda: services.ask(membership=tenant_a.admin, conversation=conversation, text="x"),
            lambda: services.mark_helpful(membership=tenant_a.admin, conversation=conversation, helpful="no"),
            lambda: services.propose_summary(membership=tenant_a.admin, conversation=conversation),
        ):
            with pytest.raises(PermissionDeniedError):
                attempt()


def test_module_only_in_pro_and_enterprise_with_quotas(db):
    extras = {code: extra for code, _name, *_quota, extra in DEFAULT_PLANS}
    assert {code for code, extra in extras.items() if "assistant" in extra} == {"pro", "enterprise"}
    assert ASSISTANT_CONVERSATION_QUOTAS == {"pro": 100, "enterprise": 500}
    # La migración de datos lo agregó a los planes existentes, sin duplicar, con su cupo.
    for code, quota in ASSISTANT_CONVERSATION_QUOTAS.items():
        plan = Plan.objects.get(code=code)
        assert plan.features.count("assistant") == 1
        assert plan.max_assistant_conversations == quota
    for code in ("trial", "basic"):
        assert "assistant" not in Plan.objects.get(code=code).features


@pytest.mark.parametrize("code", ["trial", "basic"])
def test_services_require_the_module(tenant_a, code):
    tenant_a.org.plan = Plan.objects.get(code=code)
    tenant_a.org.save(update_fields=["plan"])
    assert not has_module(tenant_a.org, "assistant")
    with tenant_context(tenant_a.org), pytest.raises(ModuleNotIncludedError):
        services.start_conversation(membership=tenant_a.tech, asset=tenant_a.asset, text="Ruido")


def test_turn_fails_cleanly_if_the_plan_loses_the_module(tenant_a):
    with tenant_context(tenant_a.org):
        conversation = services.start_conversation(
            membership=tenant_a.tech, asset=tenant_a.asset, text="Ruido"
        )
        tenant_a.org.plan = Plan.objects.get(code="basic")
        tenant_a.org.save(update_fields=["plan"])
        answer = services.run_turn(message=conversation.messages.get(seq=2))
    assert (answer.status, answer.error_code) == ("error", "module")


def test_assistant_routes_belong_to_the_module():
    assert module_for_path("/assistant/") == "assistant"
    assert module_for_path("/assistant/abc/") == "assistant"
