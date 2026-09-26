"""Alta de organización en el admin con invitación al primer administrador (ORG-01 CA2)."""

import pytest
from django.core import mail
from django.test import TestCase
from django.urls import reverse

from apps.accounts.models import Invitation, Membership, User
from apps.accounts.permissions import PermissionDeniedError
from apps.accounts.selectors import invitation_by_token
from apps.accounts.services import invite_first_admin
from apps.organizations.models import Organization, Plan
from tests.conftest import PASSWORD
from tests.test_invitations import token_from_mail


def _organization_form(**extra):
    return {
        "name": "Nueva Planta",
        "slug": "nueva-planta",
        "plan": Plan.objects.get_or_create(code="trial", defaults={"name": "Prueba", "max_users": 10})[0].pk,
        "status": Organization.Status.TRIAL,
        "trial_ends_at_0": "",
        "trial_ends_at_1": "",
        "timezone": "America/Santiago",
        "is_active": "on",
        **extra,
    }


@pytest.fixture
def superuser(db):
    return User.objects.create_superuser("root@galbust.cl", PASSWORD)


def test_platform_admin_creates_organization_with_first_admin_invitation(client, superuser):
    client.force_login(superuser)
    with TestCase.captureOnCommitCallbacks(execute=True):
        response = client.post(
            reverse("admin:organizations_organization_add"),
            _organization_form(first_admin_email="Jefe@Planta.cl"),
        )
    assert response.status_code == 302, response.content.decode()
    organization = Organization.objects.get(slug="nueva-planta")
    invitation = Invitation.objects.get(organization=organization)
    assert invitation.email == "jefe@planta.cl"
    assert invitation.role == Membership.Role.ADMIN
    assert invitation.invited_by == superuser
    assert mail.outbox[-1].to == ["jefe@planta.cl"]
    # El enlace del correo es el de una invitación normal (mismo servicio, token con hash).
    assert invitation_by_token(token_from_mail(mail.outbox[-1])) == invitation


def test_organization_without_admin_email_sends_no_invitation(client, superuser):
    client.force_login(superuser)
    response = client.post(reverse("admin:organizations_organization_add"), _organization_form())
    assert response.status_code == 302
    assert Organization.objects.filter(slug="nueva-planta").exists()
    assert not Invitation.objects.exists()
    assert not mail.outbox


def test_only_platform_admin_can_issue_the_first_admin_invitation(tenant_a):
    with pytest.raises(PermissionDeniedError):
        invite_first_admin(
            actor=tenant_a.admin.user, organization=tenant_a.org, email="x@y.cl", base_url="http://t/"
        )
    assert not Invitation.objects.exists()
