"""Auditoría de OT, solicitudes, equipo y tokens (X-01): cada acción deja su evento."""

import pytest
from django.test import TestCase
from django.urls import reverse

from apps.accounts.models import Invitation, Membership, User
from apps.accounts.services import (
    accept_invitation,
    invite_member,
    revoke_invitation,
    set_member_active,
    update_member,
)
from apps.api.services import create_token, revoke_token
from apps.audit.models import AuditEvent
from apps.work.models import WorkOrder
from apps.work.services import (
    add_checklist_item,
    add_other_cost,
    add_part_usage,
    complete_work_order,
    create_work_order,
    log_labor,
    merge_request,
    remove_cost_line,
    report_failure,
    set_assignees,
    transition,
)
from tests.conftest import login

Action = AuditEvent.Action


def _wo(t):
    return create_work_order(work_type="corrective", title="Rodamiento", asset=t.asset, by=t.admin.user)


def create(t):
    return _wo(t), Action.CREATE


def assign(t):
    wo = _wo(t)
    set_assignees(work_order=wo, membership=t.admin, assigned_to=t.tech.user)
    return wo, Action.ASSIGN


def transition_(t):
    wo = _wo(t)
    transition(work_order=wo, to_status=WorkOrder.Status.IN_PROGRESS, membership=t.tech)
    return wo, Action.TRANSITION


def complete_and_close(t):
    wo = _wo(t)
    complete_work_order(work_order=wo, membership=t.tech, labor_minutes=0)
    assert AuditEvent.objects.filter(target_id=str(wo.pk), action=Action.CLOSE).exists()
    return wo, Action.COMPLETE


def labor(t):
    wo = _wo(t)
    log_labor(work_order=wo, membership=t.tech, minutes=30)
    return wo, Action.COST_ADD


def part_removed(t):
    wo = _wo(t)
    part = add_part_usage(work_order=wo, membership=t.tech, description="Rodamiento", quantity=1, unit_cost=5)
    remove_cost_line(line=part, membership=t.tech)
    return wo, Action.COST_REMOVE


def other_cost(t):
    wo = _wo(t)
    add_other_cost(work_order=wo, membership=t.admin, description="Grúa", amount=100)
    return wo, Action.COST_ADD


def checklist(t):
    wo = _wo(t)
    add_checklist_item(work_order=wo, membership=t.admin, text="Revisar correa")
    return wo, Action.CHECKLIST_EDIT


def request_created(t):
    return report_failure(membership=t.viewer, description="Ruido", asset=t.asset), Action.CREATE


def request_merged(t):
    first = report_failure(membership=t.viewer, description="Ruido", asset=t.asset)
    second = report_failure(membership=t.viewer, description="Ruido otra vez", asset=t.asset)
    merge_request(work_request=second, into=first, membership=t.admin)
    return second, Action.TRANSITION


def invitation_sent_and_revoked(t):
    with TestCase.captureOnCommitCallbacks(execute=True):
        invitation = invite_member(
            membership=t.admin, email="x@correo.cl", role="technician", base_url="http://t/"
        )
    assert AuditEvent.objects.filter(target_id=str(invitation.pk), action=Action.INVITE).exists()
    revoke_invitation(membership=t.admin, invitation_id=invitation.pk)
    return invitation, Action.INVITE_REVOKE


def invitation_accepted(t):
    invitation = invite_member(
        membership=t.admin, email="y@correo.cl", role="technician", base_url="http://t/"
    )
    user = User.objects.create_user("y@correo.cl", "clave-larga-123")
    membership = accept_invitation(invitation=Invitation.objects.get(pk=invitation.pk), user=user)
    return membership, Action.INVITE_ACCEPT


def role_changed(t):
    update_member(
        membership=t.admin, target=t.tech, role="supervisor", restricted_to_locations=False, locations=[]
    )
    return t.tech, Action.ROLE_CHANGE


def deactivated(t):
    set_member_active(membership=t.admin, target=t.tech, active=False)
    return t.tech, Action.DEACTIVATE


def token_revoked(t):
    token, _raw = create_token(membership=t.admin, name="ERP", scope="read")
    revoke_token(membership=t.admin, token=token)
    return token, Action.REVOKE


@pytest.mark.parametrize(
    "scenario",
    [
        create,
        assign,
        transition_,
        complete_and_close,
        labor,
        part_removed,
        other_cost,
        checklist,
        request_created,
        request_merged,
        invitation_sent_and_revoked,
        invitation_accepted,
        role_changed,
        deactivated,
        token_revoked,
    ],
    ids=lambda f: f.__name__,
)
def test_action_is_audited(in_a, scenario):
    target, action = scenario(in_a)
    event = AuditEvent.objects.get(target_id=str(target.pk), action=action)
    assert event.organization_id == in_a.org.pk
    assert event.changes


def test_work_order_create_is_recorded_once(in_a):
    wo = _wo(in_a)
    assert AuditEvent.objects.filter(target_id=str(wo.pk)).count() == 1
    event = AuditEvent.objects.get(target_id=str(wo.pk))
    assert event.actor == in_a.admin.user
    assert event.changes["title"] == "Rodamiento"


def test_member_role_change_without_changes_is_not_audited(in_a):
    update_member(
        membership=in_a.admin,
        target=in_a.tech,
        role="technician",
        restricted_to_locations=False,
        locations=[],
    )
    assert not AuditEvent.objects.filter(target_id=str(in_a.tech.pk)).exists()


def test_audit_list_filters_by_new_actions(client, in_a):
    wo = _wo(in_a)
    set_assignees(work_order=wo, membership=in_a.admin, assigned_to=in_a.tech.user)
    login(client, in_a.admin)
    response = client.get(reverse("audit_list"), {"action": Action.ASSIGN})
    assert response.status_code == 200
    events = list(response.context["page"].object_list)
    assert events and {e.action for e in events} == {Action.ASSIGN}


def test_membership_audit_stays_in_its_organization(in_a, tenant_b):
    set_member_active(membership=in_a.admin, target=in_a.tech, active=False)
    event = AuditEvent.all_tenants.get(target_id=str(in_a.tech.pk))
    assert event.organization_id == in_a.org.pk != tenant_b.org.pk
    assert Membership.objects.get(pk=in_a.tech.pk).is_active is False
