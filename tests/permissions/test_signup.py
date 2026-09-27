"""ORG-02: el registro es público; con sesión iniciada no se vuelve a registrar."""

from django.urls import reverse

from tests.conftest import login


def test_anonymous_can_open_signup(client, db):
    assert client.get(reverse("signup")).status_code == 200


def test_signed_in_user_is_sent_home(client, tenant_a):
    login(client, tenant_a.tech)
    response = client.get(reverse("signup"))
    assert response.status_code == 302 and response["Location"] == reverse("dashboard")


def test_sent_page_needs_a_pending_signup(client, db):
    assert client.get(reverse("signup_sent"))["Location"] == reverse("signup")
