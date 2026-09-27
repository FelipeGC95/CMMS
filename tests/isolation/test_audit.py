"""Aislamiento de la auditoría: la organización A no ve eventos de B."""

from django.urls import reverse

from apps.audit.models import AuditEvent
from apps.audit.services import record
from apps.core.tenancy import tenant_context
from tests.conftest import login


def _record_in(tenant):
    with tenant_context(tenant.org):
        return record(action=AuditEvent.Action.CREATE, target=tenant.asset)


def test_organization_does_not_see_other_organization_events(tenant_a, tenant_b):
    own, other = _record_in(tenant_a), _record_in(tenant_b)
    with tenant_context(tenant_a.org):
        visible = set(AuditEvent.objects.values_list("pk", flat=True))
        organizations = set(AuditEvent.objects.values_list("organization_id", flat=True))
    assert own.pk in visible
    assert other.pk not in visible
    assert organizations == {tenant_a.org.id}


def test_admin_does_not_show_other_organization_events(client, tenant_a, tenant_b):
    _record_in(tenant_a)
    other = _record_in(tenant_b)
    user = tenant_a.admin.user
    user.is_staff = user.is_superuser = True
    user.save()
    login(client, tenant_a.admin)

    changelist = client.get(reverse("admin:audit_auditevent_changelist"))
    assert changelist.status_code == 200
    with tenant_context(tenant_a.org):
        own_count = AuditEvent.objects.count()
    assert changelist.context["cl"].result_count == own_count
    detail = client.get(reverse("admin:audit_auditevent_change", args=[other.pk]))
    assert detail.status_code == 302  # el admin no encuentra el objeto y redirige


def test_audit_list_view_does_not_show_other_organization_events(client, tenant_a, tenant_b):
    own, other = _record_in(tenant_a), _record_in(tenant_b)
    login(client, tenant_a.admin)
    page = client.get(reverse("audit_list")).context["page"]
    with tenant_context(tenant_a.org):  # queryset perezoso: se evalúa aquí, con RLS
        shown = {event.pk for event in page.paginator.object_list}
    assert own.pk in shown
    assert other.pk not in shown
