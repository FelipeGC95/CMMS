"""Enlaces de notificación con organización (`?org=`, ADR-003) y preferencias por rol (X-02)."""

import datetime

import pytest
from django.core import mail
from django.urls import reverse

from apps.accounts.models import Membership, User
from apps.core.middleware import SESSION_KEY
from apps.core.tenancy import tenant_context
from apps.notifications.models import Kind, NotificationPreference
from apps.notifications.selectors import preference_rows, receivable_kinds
from apps.notifications.services import email_link, save_preferences, send_due_notifications
from apps.work.services import create_work_order
from tests.conftest import PASSWORD, login

NAVIGATE = {
    "HTTP_SEC_FETCH_DEST": "document",
    "HTTP_SEC_FETCH_MODE": "navigate",
    "HTTP_SEC_FETCH_SITE": "cross-site",
}


@pytest.fixture
def two_orgs(tenant_a, tenant_b):
    """El técnico de A también es técnico en B; su organización activa es A."""
    Membership.objects.create(user=tenant_a.tech.user, organization=tenant_b.org, role="technician")
    # Sin esto la organización inicial dependería del orden de las membresías.
    User.objects.filter(pk=tenant_a.tech.user.pk).update(last_organization=tenant_a.org)
    return tenant_a, tenant_b


def _wo(tenant, **kwargs):
    with tenant_context(tenant.org):
        return create_work_order(work_type="corrective", title="Rodamiento", asset=tenant.asset, **kwargs)


# --- Enlaces de los correos -----------------------------------------------------


def test_email_links_carry_the_organization(tenant_a, settings, django_capture_on_commit_callbacks):
    settings.SITE_URL = "https://app.example.cl"
    with tenant_context(tenant_a.org), django_capture_on_commit_callbacks(execute=True):
        wo = create_work_order(
            work_type="corrective", title="R", asset=tenant_a.asset, assigned_to=tenant_a.tech.user
        )
    body = mail.outbox[0].body
    detail = reverse("work_order_detail", args=[wo.pk])
    assert f"https://app.example.cl{detail}?org={tenant_a.org.pk}" in body
    assert f"{reverse('notification_preferences')}?org={tenant_a.org.pk}" in body
    assert f"?org={tenant_a.org.pk}" in mail.outbox[0].alternatives[0][0]


def test_digest_links_carry_the_organization(tenant_a):
    _wo(tenant_a, assigned_to=tenant_a.tech.user, due_on=datetime.date(2026, 9, 20))
    with tenant_context(tenant_a.org):
        send_due_notifications(today=datetime.date(2026, 9, 24))
    assert f"?org={tenant_a.org.pk}" in mail.outbox[0].body


def test_email_link_keeps_query_and_fragment(tenant_a, settings):
    settings.SITE_URL = "https://app.example.cl/"
    with tenant_context(tenant_a.org):
        assert (
            email_link("/settings/#export")
            == f"https://app.example.cl/settings/?org={tenant_a.org.pk}#export"
        )
        assert email_link("/x/?a=1") == f"https://app.example.cl/x/?a=1&org={tenant_a.org.pk}"


# --- Cambio de organización al abrir el enlace ----------------------------------


def test_link_switches_to_the_organization_and_strips_the_parameter(client, two_orgs):
    tenant_a, tenant_b = two_orgs
    wo = _wo(tenant_b)
    login(client, tenant_a.tech)
    client.get(reverse("dashboard"))
    assert client.session[SESSION_KEY] == str(tenant_a.org.pk)
    detail = reverse("work_order_detail", args=[wo.pk])

    response = client.get(detail, {"org": str(tenant_b.org.pk), "tab": "x"}, **NAVIGATE)
    assert response.status_code == 302 and response.url == f"{detail}?tab=x"
    assert client.session[SESSION_KEY] == str(tenant_b.org.pk)
    tenant_a.tech.user.refresh_from_db()
    assert tenant_a.tech.user.last_organization_id == tenant_b.org.pk
    page = client.get(response.url)
    assert page.status_code == 200 and wo.code in page.content.decode()


def test_link_to_the_active_organization_only_strips_the_parameter(client, tenant_a):
    login(client, tenant_a.tech)
    response = client.get(reverse("dashboard"), {"org": str(tenant_a.org.pk)})
    assert response.status_code == 302 and response.url == reverse("dashboard")
    assert client.session[SESSION_KEY] == str(tenant_a.org.pk)


def test_link_without_membership_is_ignored(client, tenant_a, tenant_b):
    login(client, tenant_a.tech)
    client.get(reverse("dashboard"))
    response = client.get(reverse("dashboard"), {"org": str(tenant_b.org.pk)}, **NAVIGATE)
    # La misma respuesta que con una organización propia: no revela si existe.
    assert response.status_code == 302 and response.url == reverse("dashboard")
    assert client.session[SESSION_KEY] == str(tenant_a.org.pk)
    tenant_a.tech.user.refresh_from_db()
    assert tenant_a.tech.user.last_organization_id != tenant_b.org.pk


@pytest.mark.parametrize(
    "headers",
    [
        {"HTTP_SEC_FETCH_DEST": "image", "HTTP_SEC_FETCH_MODE": "no-cors"},
        {"HTTP_SEC_FETCH_DEST": "iframe", "HTTP_SEC_FETCH_MODE": "navigate"},
        {"HTTP_SEC_FETCH_DEST": "empty", "HTTP_SEC_FETCH_MODE": "cors"},
        {"HTTP_HX_REQUEST": "true"},
    ],
)
def test_background_requests_never_switch(client, two_orgs, headers):
    tenant_a, tenant_b = two_orgs
    login(client, tenant_a.tech)
    client.get(reverse("dashboard"))
    client.get(reverse("notification_badge"), {"org": str(tenant_b.org.pk)}, **headers)
    assert client.session[SESSION_KEY] == str(tenant_a.org.pk)


def test_post_never_switches(client, two_orgs):
    tenant_a, tenant_b = two_orgs
    login(client, tenant_a.tech)
    client.get(reverse("dashboard"))
    client.post(reverse("notification_read_all") + f"?org={tenant_b.org.pk}")
    assert client.session[SESSION_KEY] == str(tenant_a.org.pk)


def test_anonymous_keeps_the_parameter_through_login(client, two_orgs):
    tenant_a, tenant_b = two_orgs
    wo = _wo(tenant_b)
    user = tenant_a.tech.user
    user.last_organization = tenant_a.org
    user.save()
    user.emailaddress_set.create(email=user.email, verified=True, primary=True)
    detail = reverse("work_order_detail", args=[wo.pk])
    response = client.get(detail, {"org": str(tenant_b.org.pk)}, **NAVIGATE)
    assert response.status_code == 302 and "login" in response.url
    assert f"org%3D{tenant_b.org.pk}" in response.url

    response = client.post(
        response.url, {"login": tenant_a.tech.user.email, "password": PASSWORD}, follow=True
    )
    assert response.status_code == 200 and wo.code in response.content.decode()
    assert response.redirect_chain[-1][0] == detail
    assert client.session[SESSION_KEY] == str(tenant_b.org.pk)


def test_user_without_any_membership_is_just_redirected(client, db, tenant_b):
    user = User.objects.create_user("sin-org@correo.cl", PASSWORD)
    client.force_login(user)
    response = client.get(reverse("dashboard"), {"org": str(tenant_b.org.pk)})
    assert response.status_code == 302 and response.url == reverse("dashboard")
    assert SESSION_KEY not in client.session


# --- Preferencias según el rol --------------------------------------------------


def test_receivable_kinds_by_role(tenant_a):
    assert set(receivable_kinds(membership=tenant_a.admin)) == set(Kind.values)
    tech = set(receivable_kinds(membership=tenant_a.tech))
    assert Kind.REQUEST_NEW not in tech and Kind.DATA_EXPORT_READY not in tech
    assert Kind.WO_ASSIGNED in tech and Kind.REQUEST_RESOLVED in tech
    assert receivable_kinds(membership=tenant_a.viewer) == [Kind.REQUEST_RESOLVED]


def test_preferences_page_shows_only_receivable_kinds_with_a_note(client, tenant_a):
    login(client, tenant_a.tech)
    page = client.get(reverse("notification_preferences")).content.decode()
    assert f"{Kind.WO_ASSIGNED}-email" in page and "tu rol puede recibir" in page
    assert f"{Kind.REQUEST_NEW}-email" not in page and "Nueva solicitud" not in page
    login(client, tenant_a.admin)
    assert f"{Kind.REQUEST_NEW}-email" in client.get(reverse("notification_preferences")).content.decode()


def test_saved_preferences_of_hidden_kinds_are_kept(client, tenant_a):
    # Guardada cuando era supervisor; ahora es técnico.
    with tenant_context(tenant_a.org):
        NotificationPreference.objects.create(user=tenant_a.tech.user, kind=Kind.REQUEST_NEW, email=False)
    login(client, tenant_a.tech)
    client.post(reverse("notification_preferences"), {f"{Kind.WO_ASSIGNED}-in_app": "on"})
    with tenant_context(tenant_a.org):
        save_preferences(membership=tenant_a.tech, values={Kind.REQUEST_NEW: (False, False)})
        pref = NotificationPreference.objects.get(user=tenant_a.tech.user, kind=Kind.REQUEST_NEW)
        assert (pref.in_app, pref.email) == (True, False)
        assert all(row["kind"] != Kind.REQUEST_NEW for row in preference_rows(membership=tenant_a.tech))
