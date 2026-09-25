"""Invitaciones (ORG-05) y gestión del equipo: rol, alcance y estado (ORG-06)."""

import datetime
import re

import pytest
from allauth.account.models import EmailAddress
from django.core import mail
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Invitation, Membership, MembershipLocation, User
from apps.accounts.selectors import hash_token
from apps.accounts.services import TeamError, invite_member, set_member_active, update_member
from apps.core.tenancy import tenant_context
from apps.organizations.models import Location, Plan
from tests.conftest import PASSWORD, login

BASE_URL = "http://testserver/"


def invite(tenant, email="nuevo@correo.cl", role="technician"):
    with TestCase.captureOnCommitCallbacks(execute=True):
        return invite_member(membership=tenant.admin, email=email, role=role, base_url=BASE_URL)


def token_from_mail(message):
    return re.search(r"/invitations/([^/\s]+)/", message.body).group(1)


def send(client, tenant, email="nuevo@correo.cl", role="technician"):
    """Invita desde la vista (como lo hace el administrador) y devuelve el token del correo."""
    login(client, tenant.admin)
    # El correo se envía en transaction.on_commit.
    with TestCase.captureOnCommitCallbacks(execute=True):
        client.post(reverse("team"), {"email": email, "role": role})
    client.logout()
    return token_from_mail(mail.outbox[-1])


def test_invitation_mail_has_single_use_link_and_token_is_stored_hashed(client, tenant_a):
    token = send(client, tenant_a)
    invitation = Invitation.objects.get(email="nuevo@correo.cl")
    assert mail.outbox[-1].to == ["nuevo@correo.cl"]
    assert invitation.token_hash == hash_token(token) != token
    assert invitation.expires_at - timezone.now() > datetime.timedelta(days=6, hours=23)


def test_new_user_accepts_and_enters_the_organization(client, tenant_a):
    token = send(client, tenant_a, role="supervisor")
    url = reverse("invitation_accept", args=[token])
    response = client.post(
        url,
        {
            "full_name": "Nueva Persona",
            "password1": "una-clave-segura-2026",
            "password2": "una-clave-segura-2026",
        },
    )
    assert response.status_code == 302
    user = User.objects.get(email="nuevo@correo.cl")
    assert EmailAddress.objects.get(user=user).verified
    assert Membership.objects.get(user=user).role == "supervisor"
    assert client.get(reverse("dashboard")).context["organization"] == tenant_a.org
    # Un solo uso.
    client.logout()
    assert client.get(url).status_code == 404


def test_existing_account_gets_membership_without_new_user(client, tenant_a, tenant_b):
    existing = tenant_b.admin.user
    token = send(client, tenant_a, email=existing.email.upper())
    url = reverse("invitation_accept", args=[token])
    users = User.objects.count()

    response = client.get(url)
    assert response.status_code == 302 and response["Location"].startswith(reverse("account_login"))

    login(client, tenant_b.admin)
    client.post(url)
    assert User.objects.count() == users
    assert Membership.objects.filter(user=existing, organization=tenant_a.org, role="technician").exists()


def test_other_logged_in_user_cannot_accept(client, tenant_a):
    token = send(client, tenant_a)
    login(client, tenant_a.viewer)
    response = client.post(reverse("invitation_accept", args=[token]))
    assert response.context["wrong_user"]
    assert not Invitation.objects.get(email="nuevo@correo.cl").accepted_at


def test_expired_or_revoked_link_is_invalid(client, tenant_a):
    token = send(client, tenant_a)
    invitation = Invitation.objects.get(email="nuevo@correo.cl")
    Invitation.objects.filter(pk=invitation.pk).update(
        expires_at=timezone.now() - datetime.timedelta(minutes=1)
    )
    assert client.get(reverse("invitation_accept", args=[token])).status_code == 404

    token = send(client, tenant_a, email="otro@correo.cl")
    login(client, tenant_a.admin)
    client.post(reverse("invitation_revoke", args=[Invitation.objects.get(email="otro@correo.cl").pk]))
    client.logout()
    assert client.get(reverse("invitation_accept", args=[token])).status_code == 404


def test_reinviting_replaces_the_previous_link(client, tenant_a):
    first = send(client, tenant_a)
    second = send(client, tenant_a)
    assert client.get(reverse("invitation_accept", args=[first])).status_code == 404
    assert client.get(reverse("invitation_accept", args=[second])).status_code == 200


def test_cannot_invite_an_active_member(tenant_a):
    with pytest.raises(TeamError):
        invite(tenant_a, email=tenant_a.tech.user.email)


def test_user_quota_counts_pending_invitations(client, tenant_a):
    tenant_a.org.plan = Plan.objects.create(code="mini", name="Mini", max_users=4)
    tenant_a.org.save()
    invite(tenant_a, email="cuarto@correo.cl")  # 3 miembros + 1 pendiente = 4
    login(client, tenant_a.admin)
    outbox = len(mail.outbox)
    response = client.post(reverse("team"), {"email": "quinto@correo.cl", "role": "viewer"})
    assert "hasta 4 usuarios" in response.content.decode()
    assert not Invitation.objects.filter(email="quinto@correo.cl").exists()
    assert len(mail.outbox) == outbox


def _locations():
    site = Location.objects.create(code="S1", name="Planta 1")
    area = Location.objects.create(code="A1", name="Bodega", kind="area", parent=site)
    return site, area


def test_admin_sets_role_and_site_scope(client, tenant_a):
    with tenant_context(tenant_a.org):
        site, _ = _locations()
    login(client, tenant_a.admin)
    response = client.post(
        reverse("member_edit", args=[tenant_a.tech.pk]),
        {"role": "external", "restricted_to_locations": "on", "locations": [str(site.pk)]},
    )
    assert response.status_code == 302
    tenant_a.tech.refresh_from_db()
    assert tenant_a.tech.role == "external" and tenant_a.tech.restricted_to_locations
    with tenant_context(tenant_a.org):
        assert list(MembershipLocation.objects.values_list("location", flat=True)) == [site.pk]


def test_external_requires_locations(in_a):
    with pytest.raises(TeamError):
        update_member(
            membership=in_a.admin,
            target=in_a.tech,
            role="external",
            restricted_to_locations=False,
            locations=[],
        )


def test_last_admin_cannot_be_demoted_or_deactivated(in_a):
    with pytest.raises(TeamError):
        update_member(
            membership=in_a.admin,
            target=in_a.admin,
            role="viewer",
            restricted_to_locations=False,
            locations=[],
        )
    with pytest.raises(TeamError):
        set_member_active(membership=in_a.admin, target=in_a.admin, active=False)


def test_deactivate_and_reactivate_member(client, tenant_a):
    login(client, tenant_a.admin)
    client.post(reverse("member_active", args=[tenant_a.tech.pk]), {"active": "0"})
    tenant_a.tech.refresh_from_db()
    assert not tenant_a.tech.is_active
    client.post(reverse("member_active", args=[tenant_a.tech.pk]), {"active": "1"})
    tenant_a.tech.refresh_from_db()
    assert tenant_a.tech.is_active


@pytest.mark.parametrize("role", ["planner", "supervisor", "technician", "viewer", "external"])
def test_only_admins_manage_the_team(client, tenant_a, role):
    member = tenant_a.tech
    member.role = role
    member.save()
    login(client, member)
    assert client.get(reverse("team")).status_code == 403
    assert client.post(reverse("team"), {"email": "x@correo.cl", "role": "admin"}).status_code == 403
    assert (
        client.post(reverse("member_active", args=[tenant_a.viewer.pk]), {"active": "0"}).status_code == 403
    )
    assert not Invitation.objects.exists()


def test_team_page_links_only_for_admins(client, tenant_a):
    login(client, tenant_a.admin)
    assert reverse("team") in client.get(reverse("menu")).content.decode()
    client.logout()
    login(client, tenant_a.tech)
    assert reverse("team") not in client.get(reverse("menu")).content.decode()


def test_new_account_password_is_validated(client, tenant_a):
    token = send(client, tenant_a)
    response = client.post(
        reverse("invitation_accept", args=[token]), {"full_name": "X", "password1": "123", "password2": "123"}
    )
    assert response.status_code == 200
    assert not User.objects.filter(email="nuevo@correo.cl").exists()


def test_seeded_password_constant_is_still_valid_for_login(tenant_a):
    # Guarda contra cambios en los validadores que rompan la clave de prueba de conftest.
    assert tenant_a.admin.user.check_password(PASSWORD)
