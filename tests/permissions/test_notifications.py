"""Permisos de notificaciones (X-02): cada persona ve y configura solo lo suyo."""

import pytest
from django.urls import reverse

from apps.accounts.models import User
from apps.core.tenancy import tenant_context
from apps.notifications.models import Kind, NotificationPreference
from tests.conftest import PASSWORD, login

URLS = ["notification_list", "notification_badge", "notification_preferences"]


@pytest.mark.parametrize("url_name", URLS)
def test_anonymous_is_sent_to_login(client, tenant_a, url_name):
    response = client.get(reverse(url_name))
    assert response.status_code == 302 and "login" in response.url


@pytest.mark.parametrize("url_name", URLS)
def test_user_without_organization_is_refused(client, db, url_name):
    user = User.objects.create_user("sin-org@correo.cl", PASSWORD)
    client.force_login(user)
    assert client.get(reverse(url_name)).status_code == 403


@pytest.mark.parametrize("role", ["admin", "technician", "viewer"])
def test_every_role_has_its_own_inbox_and_preferences(client, tenant_a, role):
    membership = {"admin": tenant_a.admin, "technician": tenant_a.tech, "viewer": tenant_a.viewer}[role]
    login(client, membership)
    for url_name in URLS:
        assert client.get(reverse(url_name)).status_code == 200


def test_preferences_post_only_changes_own_rows(client, tenant_a):
    login(client, tenant_a.admin)
    # Un campo con el ID de otra persona no tiene efecto: el formulario no recibe usuario.
    client.post(
        reverse("notification_preferences"),
        {"user": str(tenant_a.tech.user.pk), f"{Kind.REQUEST_NEW}-email": "on"},
    )
    with tenant_context(tenant_a.org):
        assert not NotificationPreference.objects.filter(user=tenant_a.tech.user).exists()
        assert NotificationPreference.objects.filter(user=tenant_a.admin.user).count() == len(Kind.values)


def test_mark_read_requires_post(client, tenant_a):
    login(client, tenant_a.tech)
    assert client.get(reverse("notification_read_all")).status_code == 405


@pytest.mark.parametrize(
    ("role", "hidden"),
    [
        ("technician", {Kind.REQUEST_NEW, Kind.DATA_EXPORT_READY}),
        ("viewer", {Kind.REQUEST_NEW, Kind.DATA_EXPORT_READY, Kind.WO_ASSIGNED, Kind.WO_OVERDUE}),
    ],
)
def test_role_cannot_configure_kinds_it_cannot_receive(client, tenant_a, role, hidden):
    membership = {"technician": tenant_a.tech, "viewer": tenant_a.viewer}[role]
    login(client, membership)
    page = client.get(reverse("notification_preferences")).content.decode()
    assert not any(f'name="{kind}-email"' in page for kind in hidden)
    client.post(reverse("notification_preferences"), {f"{kind}-email": "on" for kind in hidden})
    with tenant_context(tenant_a.org):
        assert not NotificationPreference.objects.filter(user=membership.user, kind__in=hidden).exists()
