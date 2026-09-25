"""Pantalla del catálogo de falla (REQ-03 CA1): lista, crear, renombrar, (des)activar."""

import pytest
from django.urls import reverse

from apps.accounts.models import Membership, User
from apps.core.tenancy import tenant_context
from apps.work.models import FailureCode
from apps.work.services import create_failure_code
from tests.conftest import PASSWORD, login

K = FailureCode.Kind


def test_list_shows_seeded_codes(client, in_a):
    login(client, in_a.admin)
    shown = {c.code for c in client.get(reverse("failure_code_list")).context["page"].object_list}
    assert "BRD" in shown and "DES" in shown


def test_list_filters_by_kind(client, in_a):
    login(client, in_a.admin)
    shown = {
        c.kind for c in client.get(reverse("failure_code_list"), {"kind": "mode"}).context["page"].object_list
    }
    assert shown == {"mode"}


def test_create_view_adds_a_code(client, in_a):
    login(client, in_a.admin)
    response = client.post(
        reverse("failure_code_create"), {"kind": "mode", "code": "new", "name": "Nuevo modo"}
    )
    assert response.status_code == 302
    assert FailureCode.objects.filter(kind="mode", code="NEW", name="Nuevo modo").exists()


def test_create_view_rejects_duplicate_with_a_message(client, in_a):
    login(client, in_a.admin)
    client.post(reverse("failure_code_create"), {"kind": "mode", "code": "DUP", "name": "Uno"})
    response = client.post(
        reverse("failure_code_create"), {"kind": "mode", "code": "DUP", "name": "Otro"}, follow=True
    )
    assert any("ya existe" in str(m).lower() for m in response.context["messages"])
    assert FailureCode.objects.filter(kind="mode", code="DUP").count() == 1


def test_rename_view(client, in_a):
    code = create_failure_code(kind=K.MODE, code="RNV", name="Antes", membership=in_a.admin)
    login(client, in_a.admin)
    response = client.post(reverse("failure_code_rename", args=[code.pk]), {"name": "Después"})
    assert response.status_code == 302
    code.refresh_from_db()
    assert code.name == "Después"


def test_toggle_view_deactivates_and_reactivates(client, in_a):
    code = create_failure_code(kind=K.MODE, code="TGV", name="x", membership=in_a.admin)
    login(client, in_a.admin)
    client.post(reverse("failure_code_toggle", args=[code.pk]))
    code.refresh_from_db()
    assert code.is_active is False
    client.post(reverse("failure_code_toggle", args=[code.pk]))
    code.refresh_from_db()
    assert code.is_active is True


@pytest.mark.parametrize(
    ("role", "status"),
    [
        ("admin", 200),
        ("planner", 200),
        ("supervisor", 200),
        ("technician", 403),
        ("viewer", 403),
        ("external", 403),
    ],
)
def test_failure_code_list_is_only_for_roles_that_manage_work_orders(client, tenant_a, role, status):
    user = User.objects.create_user(f"{role}-catalog@empresa-a.cl", PASSWORD)
    member = Membership.objects.create(user=user, organization=tenant_a.org, role=role)
    login(client, member)
    assert client.get(reverse("failure_code_list")).status_code == status


def test_technician_cannot_create_or_edit_the_catalog(client, in_a):
    code = create_failure_code(kind=K.MODE, code="LCK", name="x", membership=in_a.admin)
    login(client, in_a.tech)
    assert (
        client.post(reverse("failure_code_create"), {"kind": "mode", "code": "X", "name": "x"}).status_code
        == 403
    )
    assert client.post(reverse("failure_code_rename", args=[code.pk]), {"name": "y"}).status_code == 403
    assert client.post(reverse("failure_code_toggle", args=[code.pk])).status_code == 403


def test_view_hides_foreign_organization_codes(client, tenant_a, tenant_b):
    with tenant_context(tenant_b.org):
        foreign_code = FailureCode.objects.filter(kind=FailureCode.Kind.MODE).first()
    login(client, tenant_a.admin)
    shown = {c.pk for c in client.get(reverse("failure_code_list")).context["page"].object_list}
    assert foreign_code.pk not in shown
    assert (
        client.post(reverse("failure_code_rename", args=[foreign_code.pk]), {"name": "x"}).status_code == 404
    )
    assert client.post(reverse("failure_code_toggle", args=[foreign_code.pk])).status_code == 404
