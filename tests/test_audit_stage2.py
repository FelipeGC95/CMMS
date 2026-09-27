"""Auditoría etapa 2 (X-01): vista filtrable, login y planes preventivos."""

import datetime
import zoneinfo
from unittest import mock

import pytest
from django.contrib.contenttypes.models import ContentType
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Membership, User
from apps.assets.models import Asset
from apps.audit.models import AuditEvent
from apps.audit.services import record
from apps.core.tenancy import tenant_context
from apps.maintenance.models import ItemType, MaintenancePlan, PlanAssignment, PlanVersion
from apps.maintenance.services import assign_plan, create_plan, publish_version, start_draft
from tests.conftest import PASSWORD, login

Action = AuditEvent.Action


# --- Vista ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("role", "status"),
    [
        ("admin", 200),
        ("planner", 403),
        ("supervisor", 403),
        ("technician", 403),
        ("viewer", 403),
        ("external", 403),
    ],
)
def test_audit_list_is_only_for_admins(client, tenant_a, role, status):
    user = User.objects.create_user(f"{role}-audit@empresa-a.cl", PASSWORD)
    member = Membership.objects.create(user=user, organization=tenant_a.org, role=role)
    login(client, member)
    assert client.get(reverse("audit_list")).status_code == status


def test_audit_link_only_in_admin_menu(client, tenant_a):
    # Auditoría vive en "Más" (/menu/) en escritorio y móvil.
    login(client, tenant_a.admin)
    assert reverse("audit_list") in client.get(reverse("menu")).content.decode()
    login(client, tenant_a.tech)
    assert reverse("audit_list") not in client.get(reverse("menu")).content.decode()


def test_audit_list_filters(client, tenant_a):
    with tenant_context(tenant_a.org):
        asset = Asset.objects.create(code="F-1", name="Filtro", asset_class=tenant_a.asset.asset_class)
        record(action=Action.CREATE, target=asset, actor=tenant_a.admin.user)
        record(action=Action.TRANSITION, target=asset, actor=tenant_a.tech.user)
        record(action=Action.UPDATE, target=tenant_a.plan, actor=tenant_a.tech.user)
    login(client, tenant_a.admin)  # también queda auditado
    with tenant_context(tenant_a.org):
        total = AuditEvent.objects.count()
    url = reverse("audit_list")

    def count(**params):
        response = client.get(url, params)
        assert response.status_code == 200
        return response.context["page"].paginator.count

    asset_type = ContentType.objects.get_for_model(Asset).pk
    today = timezone.localdate(timezone=zoneinfo.ZoneInfo(tenant_a.org.timezone)).isoformat()
    assert count() == total
    assert count(action="transition", target_type=asset_type) == 1
    assert count(actor="technician@") == 2
    assert count(target_type=asset_type) == 2
    assert count(actor="technician@", target_type=asset_type) == 1
    assert count(date_from="2000-01-01", date_to="2000-01-02") == 0
    assert count(date_from=today) == total
    assert count(action="no-existe") == total  # filtro inválido: se ignora


def test_audit_list_dates_use_the_organization_timezone(client, tenant_a):
    """01:44 UTC del 25 de septiembre sigue siendo el 24 en Santiago (UTC-3)."""
    near_midnight = datetime.datetime(2026, 9, 25, 1, 44, tzinfo=datetime.UTC)
    with tenant_context(tenant_a.org), mock.patch("django.utils.timezone.now", return_value=near_midnight):
        event = record(action=Action.UPDATE, target=tenant_a.asset)
    login(client, tenant_a.admin)

    def shown(**params):
        page = client.get(reverse("audit_list"), params).context["page"]
        return event.pk in {e.pk for e in page.paginator.object_list}

    assert shown(date_from="2026-09-24", date_to="2026-09-24")
    assert not shown(date_from="2026-09-25")
    assert not shown(date_to="2026-09-23")


# --- Login ---------------------------------------------------------------------


def test_login_is_audited_in_the_active_organization(client, tenant_a):
    user = tenant_a.admin.user
    user.emailaddress_set.create(email=user.email, verified=True, primary=True)
    response = client.post(
        reverse("account_login"), {"login": user.email, "password": PASSWORD}, REMOTE_ADDR="10.2.2.2"
    )
    assert response.status_code == 302
    with tenant_context(tenant_a.org):
        event = AuditEvent.objects.get(action=Action.LOGIN)
    assert (event.actor, event.target_id, event.ip) == (user, str(tenant_a.admin.pk), "10.2.2.2")


def test_login_prefers_last_organization(tenant_a, tenant_b):
    from apps.audit.services import login_membership

    user = tenant_a.admin.user
    other = Membership.objects.create(user=user, organization=tenant_b.org, role="viewer")
    user.last_organization = tenant_b.org
    user.save()
    assert login_membership(user) == other


def test_login_without_organization_is_not_audited(client, db):
    user = User.objects.create_user("solo@x.cl", PASSWORD)
    user.emailaddress_set.create(email=user.email, verified=True, primary=True)
    client.post(reverse("account_login"), {"login": user.email, "password": PASSWORD})
    assert not AuditEvent.all_tenants.exists()


# --- Planes --------------------------------------------------------------------


def test_create_plan_and_publish_are_audited(in_a):
    user = in_a.admin.user
    plan = create_plan(
        code="PM-A",
        name="Semanal",
        calendar_interval=7,
        items=[{"text": "Revisar", "item_type": ItemType.CHECK}],
        by=user,
    )
    created = AuditEvent.objects.get(action=Action.CREATE, target_id=str(plan.pk))
    assert created.actor == user
    assert created.changes["code"] == "PM-A"
    published = AuditEvent.objects.get(action=Action.TRANSITION, target_id=str(plan.current_version.pk))
    assert published.changes["status"] == [PlanVersion.Status.DRAFT, PlanVersion.Status.PUBLISHED]
    assert published.changes["replaces"] is None

    draft = start_draft(plan=plan)
    publish_version(version=draft, by=user, change_note="Cambio")
    second = AuditEvent.objects.filter(action=Action.TRANSITION).get(target_id=str(draft.pk))
    assert second.changes["replaces"] == 1


def test_assign_plan_is_audited_only_when_it_changes(in_a):
    plan = MaintenancePlan.objects.get(pk=in_a.plan.pk)
    if plan.current_version is None:
        pytest.skip("el plan de la fixture no tiene versión publicada")
    starts = datetime.date(2026, 10, 1)
    assignment = assign_plan(plan=plan, asset=in_a.asset, starts_on=starts, by=in_a.admin.user)
    assign_plan(plan=plan, asset=in_a.asset, starts_on=starts, by=in_a.admin.user)
    assign_plan(plan=plan, asset=in_a.asset, starts_on=starts + datetime.timedelta(days=1))

    events = AuditEvent.objects.filter(target_id=str(assignment.pk)).order_by("created_at")
    assert [e.action for e in events] == [Action.CREATE, Action.UPDATE]
    assert events[0].actor == in_a.admin.user
    assert events[1].changes["next_due_on"] == ["2026-10-01", "2026-10-02"]
    assert PlanAssignment.objects.count() == 1
