"""ORG-02: autoregistro con prueba gratuita, verificación de correo antes de crear datos y anti-bots."""

import datetime
import re

import pytest
from django.core import mail
from django.core.cache import cache
from django.urls import reverse
from django.utils import timezone

from apps.accounts.forms import SignupForm
from apps.accounts.models import Membership, PendingSignup, User
from apps.accounts.services import SignupError, complete_signup, request_signup
from apps.organizations.models import Organization

STRONG = "una-clave-bien-larga-2026"


@pytest.fixture(autouse=True)
def _fast_signup(settings, db):
    settings.SIGNUP_MIN_SECONDS = 0
    cache.clear()


def form_data(client, **overrides):
    page = client.get(reverse("signup"))
    form = page.context["form"]
    challenge = form._challenge
    data = {
        "full_name": "Ana Pérez",
        "email": "ana@planta.cl",
        "organization_name": "Planta Sur SpA",
        "password": STRONG,
        "answer": challenge["a"] + challenge["b"],
        "challenge": form.initial["challenge"],
        "website": "",
    }
    data.update(overrides)
    return data


def confirm_url_from_mail():
    return re.search(r"https?://\S+/signup/confirm/\S+/", mail.outbox[-1].body).group(0)


def test_signup_creates_nothing_before_verification(client, db):
    response = client.post(reverse("signup"), form_data(client))
    assert response.status_code == 302 and response["Location"] == reverse("signup_sent")
    assert not User.objects.filter(email="ana@planta.cl").exists()
    assert not Organization.objects.exists()
    pending = PendingSignup.objects.get()
    assert pending.password_hash.startswith("argon2") and STRONG not in pending.password_hash
    assert len(mail.outbox) == 1 and "/signup/confirm/" in mail.outbox[0].body
    assert client.get(reverse("signup_sent")).status_code == 200


def test_verification_creates_user_org_and_admin_membership(client, db):
    client.post(reverse("signup"), form_data(client))
    url = confirm_url_from_mail()
    # Abrir el enlace (GET) no consume el registro: los antivirus del correo lo abren.
    assert client.get(url).status_code == 200
    assert not Organization.objects.exists()
    response = client.post(url)
    assert response.status_code == 302 and response["Location"] == reverse(
        "onboarding"
    )  # puesta en marcha (ORG-03)
    user = User.objects.get(email="ana@planta.cl")
    org = Organization.objects.get()
    assert org.name == "Planta Sur SpA" and org.slug == "planta-sur-spa"
    assert org.plan.code == "trial" and org.status == Organization.Status.TRIAL
    assert abs(org.trial_ends_at - (timezone.now() + datetime.timedelta(days=30))) < datetime.timedelta(
        minutes=1
    )
    membership = Membership.objects.get(user=user)
    assert membership.organization == org and membership.role == Membership.Role.ADMIN
    assert user.emailaddress_set.get().verified
    assert user.check_password(STRONG)
    assert "_auth_user_id" in client.session
    # El enlace sirve una sola vez.
    assert client.post(url).status_code == 404


def test_new_admin_is_sent_to_mfa_setup_without_password_prompt(client, db, settings):
    settings.MFA_ENFORCE = True
    client.post(reverse("signup"), form_data(client))
    client.post(confirm_url_from_mail())
    response = client.get(reverse("dashboard"))
    assert response["Location"] == reverse("mfa_activate_totp")
    assert client.get(reverse("mfa_activate_totp")).status_code == 200


def test_complete_signup_is_atomic(db, monkeypatch):
    tokens = []
    request_signup(
        email="bo@x.cl",
        full_name="Bo",
        organization_name="X",
        password=STRONG,
        confirm_url=lambda token: tokens.append(token) or "u",
        login_url="l",
    )

    def boom(**kwargs):
        raise RuntimeError("falla al crear la membresía")

    monkeypatch.setattr(Membership.objects, "create", boom)
    with pytest.raises(RuntimeError):
        complete_signup(token=tokens[0])
    assert not User.objects.exists() and not Organization.objects.exists()
    assert PendingSignup.objects.get().completed_at is None


def test_expired_link_is_rejected(db):
    tokens = []
    request_signup(
        email="c@x.cl",
        full_name="C",
        organization_name="C",
        password=STRONG,
        confirm_url=lambda t: tokens.append(t) or "u",
        login_url="l",
    )
    PendingSignup.objects.update(expires_at=timezone.now() - datetime.timedelta(minutes=1))
    with pytest.raises(SignupError):
        complete_signup(token=tokens[0])


def test_existing_email_gets_mail_but_page_does_not_reveal_it(client, tenant_a):
    email = tenant_a.admin.user.email
    response = client.post(reverse("signup"), form_data(client, email=email))
    assert response["Location"] == reverse("signup_sent")
    assert not PendingSignup.objects.exists()
    assert "ya tienes una cuenta" in mail.outbox[-1].subject.lower()


def test_duplicate_org_names_get_unique_slugs(client, db):
    for email in ("a1@x.cl", "a2@x.cl"):
        client.post(reverse("signup"), form_data(client, email=email))
        client.post(confirm_url_from_mail())
        client.logout()
    assert sorted(Organization.objects.values_list("slug", flat=True)) == [
        "planta-sur-spa",
        "planta-sur-spa-2",
    ]


@pytest.mark.parametrize(
    "overrides",
    [{"website": "http://spam"}, {"answer": 999}, {"challenge": "manipulado"}],
    ids=["honeypot", "wrong-sum", "bad-token"],
)
def test_bots_are_rejected(client, db, overrides):
    response = client.post(reverse("signup"), form_data(client, **overrides))
    assert response.status_code == 200
    assert not PendingSignup.objects.exists() and not mail.outbox


def test_forged_token_is_rejected_even_if_answer_matches_the_new_challenge(client, db, monkeypatch):
    # El desafío que se regenera para volver a mostrar el formulario no puede validar el envío.
    data = form_data(client, challenge="manipulado", answer=6)
    monkeypatch.setattr("apps.accounts.forms.secrets.randbelow", lambda n: 2)  # nuevo desafío: 3 + 3
    response = client.post(reverse("signup"), data)
    assert response.status_code == 200
    assert not PendingSignup.objects.exists() and not mail.outbox


def test_too_fast_submission_is_rejected(client, db, settings):
    settings.SIGNUP_MIN_SECONDS = 60
    response = client.post(reverse("signup"), form_data(client))
    assert response.status_code == 200 and not PendingSignup.objects.exists()


def test_weak_password_is_rejected(client, db):
    response = client.post(reverse("signup"), form_data(client, password="123"))
    assert response.status_code == 200 and not PendingSignup.objects.exists()


def test_rate_limit_per_ip(client, db, settings):
    settings.ACCOUNT_RATE_LIMITS = {"signup": "2/m/ip"}
    for i in range(2):
        client.post(reverse("signup"), form_data(client, email=f"r{i}@x.cl"))
    response = client.post(reverse("signup"), form_data(client, email="r9@x.cl"))
    assert response.status_code == 429
    assert PendingSignup.objects.count() == 2


def test_challenge_survives_redisplay():
    form = SignupForm(data={"challenge": "x"})
    assert form.data["challenge"] != "x"  # token inválido: se genera uno nuevo
    assert "¿cuánto es" in str(form.fields["answer"].label)


def test_allauth_signup_stays_closed_and_invitations_work(client, db):
    response = client.get(reverse("account_signup"))
    assert "signup" not in response.get("Location", "") or response.status_code != 200
    assert client.get(reverse("account_login")).status_code == 200
    assert reverse("signup") in client.get(reverse("account_login")).content.decode()
