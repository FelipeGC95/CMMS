"""SSO por OIDC (ORG-11, ADR-032): configuración, dominios, ingreso, alta automática y exigencia."""

from urllib.parse import parse_qs, urlsplit

import pytest
from allauth.account.models import EmailAddress
from django.core import mail
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Invitation, Membership, User
from apps.accounts.services import create_custom_role
from apps.audit.models import AuditEvent
from apps.core.tenancy import tenant_context
from apps.organizations.models import Organization, Plan
from apps.organizations.modules import ModuleNotIncludedError
from apps.sso import crypto, oidc, selectors, services
from apps.sso.models import SsoConfiguration, SsoDomain
from apps.sso.services import SsoError
from tests.conftest import PASSWORD, login
from tests.sso_utils import FakeIdP, configure, install_dns, install_idps, rsa_key, sso_round_trip


@pytest.fixture
def idp(monkeypatch):
    provider = FakeIdP("tenant-a")
    install_idps(monkeypatch, provider)
    return provider


@pytest.fixture
def dns(monkeypatch):
    return install_dns(monkeypatch)


# --- Configuración --------------------------------------------------------------------------


def test_save_configuration_validates_discovery_and_encrypts_the_secret(tenant_a, idp):
    configuration = configure(tenant_a, idp, domains=())
    assert configuration.discovery_url == idp.discovery_url
    assert configuration.provider_metadata["issuer"] == idp.issuer
    assert "secreto-del-cliente-123" not in configuration.client_secret_encrypted
    assert crypto.decrypt(configuration.client_secret_encrypted) == "secreto-del-cliente-123"
    with tenant_context(tenant_a.org):
        event = AuditEvent.objects.get(action=AuditEvent.Action.CREATE, target_id=str(configuration.pk))
    assert "secreto-del-cliente-123" not in str(event.changes)
    assert not any("secret" in key for key in event.changes)


def test_blank_secret_keeps_the_saved_one(tenant_a, idp):
    configure(tenant_a, idp, domains=())
    with tenant_context(tenant_a.org):
        services.save_configuration(
            membership=tenant_a.admin,
            discovery_url=idp.discovery_url,
            client_id="otro-cliente",
            default_role="viewer",
            is_active=True,
        )
        configuration = SsoConfiguration.objects.get()
    assert configuration.client_id == "otro-cliente"
    assert crypto.decrypt(configuration.client_secret_encrypted) == "secreto-del-cliente-123"


@pytest.mark.parametrize(
    "url, message",
    [
        ("http://idp.example.com/tenant-a", "https"),
        ("https://idp.example.com/otro", "emisor"),
    ],
)
def test_discovery_must_be_https_and_match_the_issuer(tenant_a, idp, monkeypatch, url, message):
    # Cualquier URL de descubrimiento responde con el emisor de tenant-a.
    monkeypatch.setattr(
        oidc,
        "http_request",
        lambda method, u, **kw: (
            idp.handle(method, idp.discovery_url, None) if u.endswith(oidc.DISCOVERY_SUFFIX) else (404, b"{}")
        ),
    )
    with tenant_context(tenant_a.org), pytest.raises(SsoError, match=message):
        services.save_configuration(
            membership=tenant_a.admin,
            discovery_url=url,
            client_id="x",
            client_secret="y",
            default_role="technician",
        )


def test_discovery_url_rejects_private_addresses(tenant_a, monkeypatch):
    """Sin reemplazar http_request: la defensa SSRF de los webhooks (ADR-030) rechaza la IP."""
    from apps.webhooks import services as outbound

    monkeypatch.setattr(outbound, "resolve_host", lambda host, port: ["169.254.169.254"])
    with tenant_context(tenant_a.org), pytest.raises(SsoError, match="privada"):
        services.save_configuration(
            membership=tenant_a.admin,
            discovery_url="https://metadata.example.com",
            client_id="x",
            client_secret="y",
            default_role="technician",
        )


def test_default_role_is_never_admin(tenant_a, idp):
    with tenant_context(tenant_a.org), pytest.raises(SsoError, match="administrador"):
        services.save_configuration(
            membership=tenant_a.admin,
            discovery_url=idp.issuer,
            client_id="x",
            client_secret="y",
            default_role="admin",
        )


def test_enforce_requires_active(tenant_a, idp):
    with tenant_context(tenant_a.org), pytest.raises(SsoError, match="actívalo"):
        services.save_configuration(
            membership=tenant_a.admin,
            discovery_url=idp.issuer,
            client_id="x",
            client_secret="y",
            default_role="technician",
            enforce_sso=True,
            is_active=False,
        )


def test_test_connection_records_the_result(tenant_a, idp):
    configure(tenant_a, idp, domains=())
    with tenant_context(tenant_a.org):
        assert services.test_connection(membership=tenant_a.admin).last_test_ok is True
        SsoConfiguration.objects.update(client_secret_encrypted="no-es-fernet")
        result = services.test_connection(membership=tenant_a.admin)
    assert result.last_test_ok is False and "secreto" in result.last_test_error


def test_settings_page_never_shows_the_secret(client, tenant_a, idp):
    configure(tenant_a, idp)
    login(client, tenant_a.admin)
    page = client.get(reverse("sso_settings")).content.decode()
    assert "secreto-del-cliente-123" not in page
    assert reverse("sso_callback") in page
    assert "empresa-a.cl" in page


def test_settings_form_saves_through_the_screen(client, tenant_a, idp):
    login(client, tenant_a.admin)
    response = client.post(
        reverse("sso_settings"),
        {
            "discovery_url": idp.issuer,
            "client_id": "app",
            "client_secret": "s3creto",
            "default_role": "viewer",
            "is_active": "on",
        },
    )
    assert response.status_code == 302
    with tenant_context(tenant_a.org):
        configuration = SsoConfiguration.objects.get()
    assert configuration.default_role == "viewer" and configuration.is_active


# --- Dominios ---------------------------------------------------------------------------------


def test_domain_is_verified_with_the_txt_record(tenant_a, dns):
    with tenant_context(tenant_a.org):
        domain = services.add_domain(membership=tenant_a.admin, domain="@Empresa-A.cl")
        assert domain.domain == "empresa-a.cl"
        with pytest.raises(SsoError, match="TXT"):
            services.verify_domain(membership=tenant_a.admin, domain=domain)
        domain.refresh_from_db()
        assert not domain.is_verified and domain.last_checked_at is not None
        dns[domain.txt_name] = ["v=spf1 -all", domain.txt_value]
        services.verify_domain(membership=tenant_a.admin, domain=domain)
        domain.refresh_from_db()
    assert domain.is_verified
    assert domain.txt_name == "_galbust-verification.empresa-a.cl"


@pytest.mark.parametrize("value", ["no es dominio", "gmail.com", "localhost", "https://x.cl/"])
def test_invalid_or_personal_domains_are_rejected(tenant_a, value):
    with tenant_context(tenant_a.org), pytest.raises(SsoError):
        services.add_domain(membership=tenant_a.admin, domain=value)


def test_a_verified_domain_belongs_to_one_organization(tenant_a, tenant_b, dns):
    with tenant_context(tenant_a.org):
        domain_a = services.add_domain(membership=tenant_a.admin, domain="compartido.cl")
    with tenant_context(tenant_b.org):
        # Pendiente en A no bloquea a B (nadie se adueña sin verificar).
        domain_b = services.add_domain(membership=tenant_b.admin, domain="compartido.cl")
    dns[domain_a.txt_name] = [domain_a.txt_value, domain_b.txt_value]
    with tenant_context(tenant_a.org):
        services.verify_domain(membership=tenant_a.admin, domain=domain_a)
    with tenant_context(tenant_b.org):
        with pytest.raises(SsoError, match="otra organización"):
            services.verify_domain(membership=tenant_b.admin, domain=domain_b)
        domain_b.refresh_from_db()
        assert not domain_b.is_verified
        services.remove_domain(membership=tenant_b.admin, domain=domain_b)
        with pytest.raises(SsoError, match="otra organización"):
            services.add_domain(membership=tenant_b.admin, domain="compartido.cl")


def test_remove_domain(tenant_a, dns):
    with tenant_context(tenant_a.org):
        domain = services.add_domain(membership=tenant_a.admin, domain="empresa-a.cl")
        pk = str(domain.pk)
        services.remove_domain(membership=tenant_a.admin, domain=domain)
        assert not SsoDomain.objects.exists()
        assert AuditEvent.objects.filter(action=AuditEvent.Action.DELETE, target_id=pk).exists()


# --- Ingreso ----------------------------------------------------------------------------------


def test_login_page_offers_sso(client, db):
    page = client.get(reverse("account_login")).content.decode()
    assert reverse("sso_login") in page


def test_authorization_request_uses_pkce_state_and_nonce(client, tenant_a, idp):
    configure(tenant_a, idp)
    response = client.post(reverse("sso_login"), {"email": "nueva@empresa-a.cl"})
    assert response.status_code == 302
    location = urlsplit(response["Location"])
    assert f"{location.scheme}://{location.netloc}{location.path}" == f"{idp.issuer}/authorize"
    query = {key: values[0] for key, values in parse_qs(location.query).items()}
    flow = client.session["sso_flow"]
    assert query["state"] == flow["state"] and query["nonce"] == flow["nonce"]
    assert query["code_challenge_method"] == "S256"
    assert query["code_challenge"] == oidc.code_challenge(flow["code_verifier"])
    assert query["scope"] == "openid email profile" and query["login_hint"] == "nueva@empresa-a.cl"
    assert query["redirect_uri"].endswith(reverse("sso_callback"))


def test_unknown_domain_gets_a_clear_message(client, tenant_a, idp):
    configure(tenant_a, idp)
    response = client.post(reverse("sso_login"), {"email": "x@otra.cl"})
    assert response.status_code == 400
    assert "contraseña" in response.content.decode()


def test_first_login_creates_user_and_membership_with_default_role(client, tenant_a, idp):
    configure(tenant_a, idp, default_role="viewer")
    response = sso_round_trip(client, idp, "nueva@empresa-a.cl")
    assert response.status_code == 302 and response["Location"] == reverse("dashboard")
    user = User.objects.get(email="nueva@empresa-a.cl")
    assert not user.has_usable_password()
    assert user.full_name == "Persona SSO"
    membership = Membership.objects.get(user=user, organization=tenant_a.org)
    assert membership.role == "viewer" and membership.custom_role is None
    assert EmailAddress.objects.get(user=user).verified
    assert client.session["_auth_user_id"] == str(user.pk)
    assert client.session["sso_organization_id"] == str(tenant_a.org.pk)
    # El código se canjeó con el verificador PKCE y el secreto descifrado.
    request = idp.token_requests[-1]
    assert request["code_verifier"] and request["client_secret"] == "secreto-del-cliente-123"
    with tenant_context(tenant_a.org):
        created = AuditEvent.objects.get(action=AuditEvent.Action.CREATE, target_id=str(membership.pk))
        assert created.changes["source"] == "sso"
        assert AuditEvent.objects.filter(action=AuditEvent.Action.LOGIN, actor=user).exists()
    assert client.get(reverse("dashboard")).status_code == 200


def test_existing_member_signs_in_without_changes(client, tenant_a, idp):
    configure(tenant_a, idp)
    response = sso_round_trip(client, idp, "technician@empresa-a.cl")
    assert response.status_code == 302
    assert client.session["_auth_user_id"] == str(tenant_a.tech.user.pk)
    assert Membership.objects.filter(user=tenant_a.tech.user).count() == 1
    tenant_a.tech.refresh_from_db()
    assert tenant_a.tech.role == "technician"


def test_default_custom_role_is_used(client, tenant_a, idp):
    with tenant_context(tenant_a.org):
        role = create_custom_role(membership=tenant_a.admin, name="Bodeguero", base_role="technician")
    configure(tenant_a, idp, default_role="", default_custom_role=role.pk)
    sso_round_trip(client, idp, "bodega@empresa-a.cl")
    membership = Membership.objects.get(user__email="bodega@empresa-a.cl")
    assert membership.custom_role_id == role.pk and membership.role == "technician"


def test_role_written_as_admin_in_the_database_still_is_not_admin(client, tenant_a, idp):
    configure(tenant_a, idp)
    with tenant_context(tenant_a.org):
        SsoConfiguration.objects.update(default_role="admin")
    sso_round_trip(client, idp, "nueva@empresa-a.cl")
    assert Membership.objects.get(user__email="nueva@empresa-a.cl").role == "technician"


def test_pending_invitation_role_wins(client, tenant_a, idp):
    from apps.accounts.services import invite_member

    configure(tenant_a, idp)
    with tenant_context(tenant_a.org):
        invite_member(
            membership=tenant_a.admin, email="invitada@empresa-a.cl", role="planner", base_url="http://t"
        )
    sso_round_trip(client, idp, "invitada@empresa-a.cl")
    assert Membership.objects.get(user__email="invitada@empresa-a.cl").role == "planner"
    assert Invitation.objects.get(email="invitada@empresa-a.cl").accepted_at is not None


def test_email_from_userinfo_when_id_token_has_none(client, tenant_a, idp):
    configure(tenant_a, idp)
    idp.drop_email = True
    idp.userinfo = {"sub": "sub-desde@empresa-a.cl", "email": "desde@empresa-a.cl", "email_verified": True}
    response = sso_round_trip(client, idp, "desde@empresa-a.cl")
    assert response.status_code == 302
    assert User.objects.filter(email="desde@empresa-a.cl").exists()


# --- Casos de seguridad -----------------------------------------------------------------------


def _assert_rejected(client, response, text=""):
    assert response.status_code == 400
    assert "_auth_user_id" not in client.session
    if text:
        assert text in response.content.decode()


def test_invalid_state_is_rejected(client, tenant_a, idp):
    configure(tenant_a, idp)
    _assert_rejected(client, sso_round_trip(client, idp, "nueva@empresa-a.cl", state="otro"), "state")
    assert not User.objects.filter(email="nueva@empresa-a.cl").exists()


def test_callback_without_flow_or_replayed_is_rejected(client, tenant_a, idp):
    configure(tenant_a, idp)
    _assert_rejected(
        client, client.get(reverse("sso_callback"), {"code": "code-for:x@empresa-a.cl", "state": "s"})
    )
    sso_round_trip(client, idp, "nueva@empresa-a.cl")
    client.logout()
    # El mismo state no sirve dos veces: el intento se borró de la sesión.
    response = client.get(reverse("sso_callback"), {"code": "code-for:nueva@empresa-a.cl", "state": "x"})
    _assert_rejected(client, response, "venció")


def test_expired_flow_is_rejected(client, tenant_a, idp, monkeypatch):
    configure(tenant_a, idp)
    start = client.post(reverse("sso_login"), {"email": "nueva@empresa-a.cl"})
    query = {key: values[0] for key, values in parse_qs(urlsplit(start["Location"]).query).items()}
    session = client.session
    session["sso_flow"] = {**session["sso_flow"], "created_at": 0}
    session.save()
    idp.nonce = query["nonce"]
    response = client.get(
        reverse("sso_callback"), {"code": "code-for:nueva@empresa-a.cl", "state": query["state"]}
    )
    _assert_rejected(client, response, "venció")


def test_invalid_nonce_is_rejected(client, tenant_a, idp):
    configure(tenant_a, idp)
    idp.claims = {"nonce": "otro-nonce"}
    _assert_rejected(client, sso_round_trip(client, idp, "nueva@empresa-a.cl"), "nonce")


def test_idp_error_is_shown(client, tenant_a, idp):
    configure(tenant_a, idp)
    start = client.post(reverse("sso_login"), {"email": "nueva@empresa-a.cl"})
    state = parse_qs(urlsplit(start["Location"]).query)["state"][0]
    response = client.get(reverse("sso_callback"), {"error": "access_denied", "state": state})
    _assert_rejected(client, response, "access_denied")


def test_bad_signature_is_rejected(client, tenant_a, idp):
    configure(tenant_a, idp)
    idp.signing_key = rsa_key("atacante")
    _assert_rejected(client, sso_round_trip(client, idp, "nueva@empresa-a.cl"), "firma")


def test_symmetric_algorithm_is_rejected(client, tenant_a, idp):
    configure(tenant_a, idp)
    idp.algorithm = "HS256"
    _assert_rejected(client, sso_round_trip(client, idp, "nueva@empresa-a.cl"), "algoritmo")


@pytest.mark.parametrize("claims", [{"aud": "otra-app"}, {"iss": "https://idp.example.com/otro"}])
def test_wrong_audience_or_issuer_is_rejected(client, tenant_a, idp, claims):
    configure(tenant_a, idp)
    idp.claims = claims
    _assert_rejected(client, sso_round_trip(client, idp, "nueva@empresa-a.cl"))


def test_expired_id_token_is_rejected(client, tenant_a, idp):
    configure(tenant_a, idp)
    idp.claims = {"exp": int(timezone.now().timestamp()) - 3600}
    _assert_rejected(client, sso_round_trip(client, idp, "nueva@empresa-a.cl"))


def test_email_verified_false_is_rejected(client, tenant_a, idp):
    configure(tenant_a, idp)
    idp.claims = {"email_verified": False}
    _assert_rejected(client, sso_round_trip(client, idp, "technician@empresa-a.cl"), "no está verificado")


def test_unverified_domain_is_rejected(client, tenant_a, idp):
    configure(tenant_a, idp, domains=("empresa-a.cl",))
    with tenant_context(tenant_a.org):
        services.add_domain(membership=tenant_a.admin, domain="pendiente.cl")
    # Se inicia con el dominio verificado, pero el IdP responde con un correo del dominio
    # pendiente: no se enlaza ni se crea.
    start = client.post(reverse("sso_login"), {"email": "nueva2@empresa-a.cl"})
    query = {key: values[0] for key, values in parse_qs(urlsplit(start["Location"]).query).items()}
    idp.nonce = query["nonce"]
    response = client.get(
        reverse("sso_callback"), {"code": "code-for:jefe@pendiente.cl", "state": query["state"]}
    )
    _assert_rejected(client, response, "dominio verificado")
    assert not User.objects.filter(email="jefe@pendiente.cl").exists()


def test_sso_without_verified_domain_does_not_start(client, tenant_a, idp):
    configure(tenant_a, idp, verified=False)
    assert client.post(reverse("sso_login"), {"email": "nueva@empresa-a.cl"}).status_code == 400


def test_full_quota_blocks_new_users_with_a_clear_message(client, tenant_a, idp):
    configure(tenant_a, idp)
    tenant_a.org.plan = Plan.objects.create(code="tiny", name="Mini", max_users=3, features=[])
    tenant_a.org.save(update_fields=["plan"])
    _assert_rejected(client, sso_round_trip(client, idp, "nueva@empresa-a.cl"), "máximo de usuarios")
    assert not User.objects.filter(email="nueva@empresa-a.cl").exists()
    # Quien ya es miembro sigue entrando con el cupo lleno.
    client.logout()
    assert sso_round_trip(client, idp, "viewer@empresa-a.cl").status_code == 302


def test_deactivated_member_cannot_sign_in(client, tenant_a, idp):
    configure(tenant_a, idp)
    Membership.objects.filter(pk=tenant_a.viewer.pk).update(is_active=False)
    _assert_rejected(client, sso_round_trip(client, idp, "viewer@empresa-a.cl"), "desactivado")
    assert not Membership.objects.get(pk=tenant_a.viewer.pk).is_active


def test_inactive_configuration_or_plan_without_module_disables_sso(client, tenant_a, idp):
    configure(tenant_a, idp)
    with tenant_context(tenant_a.org):
        SsoConfiguration.objects.update(is_active=False)
    assert selectors.sso_for_email("x@empresa-a.cl") is None
    with tenant_context(tenant_a.org):
        SsoConfiguration.objects.update(is_active=True)
    assert selectors.sso_for_email("x@empresa-a.cl") is not None
    tenant_a.org.plan = Plan.objects.create(code="sin-sso", name="Sin SSO", features=["assets"])
    tenant_a.org.save(update_fields=["plan"])
    assert selectors.sso_for_email("x@empresa-a.cl") is None
    with tenant_context(tenant_a.org), pytest.raises(ModuleNotIncludedError):
        services.add_domain(membership=tenant_a.admin, domain="otra.cl")


# --- Sin configuración nada cambia ------------------------------------------------------------


def test_without_configuration_password_login_works_as_before(client, tenant_a):
    response = client.post(
        reverse("account_login"), {"login": "technician@empresa-a.cl", "password": PASSWORD}
    )
    assert response.status_code == 302 and response["Location"] != reverse("sso_login")
    assert selectors.password_login_blocked(tenant_a.tech.user) is None


# --- Exigir SSO -------------------------------------------------------------------------------


@pytest.fixture
def enforced(tenant_a, idp):
    return configure(tenant_a, idp, enforce_sso=True)


def _password_login(client, email):
    return client.post(reverse("account_login"), {"login": email, "password": PASSWORD})


def test_enforced_sso_blocks_password_for_non_admins(client, tenant_a, enforced):
    response = _password_login(client, "technician@empresa-a.cl")
    assert response.status_code == 302 and response["Location"].startswith(reverse("sso_login"))
    assert "_auth_user_id" not in client.session


def test_admins_keep_password_as_emergency_access(client, tenant_a, enforced):
    user = tenant_a.admin.user
    EmailAddress.objects.create(user=user, email=user.email, primary=True, verified=True)
    response = _password_login(client, "admin@empresa-a.cl")
    assert response.status_code == 302 and not response["Location"].startswith(reverse("sso_login"))
    assert client.session["_auth_user_id"] == str(tenant_a.admin.user.pk)


def test_rule_follows_the_email_domain_not_other_memberships(client, tenant_a, tenant_b, enforced):
    """Técnico en A (dominio exigido) y admin en B: igual debe entrar por el SSO de A."""
    Membership.objects.create(user=tenant_a.tech.user, organization=tenant_b.org, role="admin")
    response = _password_login(client, "technician@empresa-a.cl")
    assert response["Location"].startswith(reverse("sso_login"))
    # Y un correo de B entra con contraseña aunque sea miembro de A.
    Membership.objects.create(user=tenant_b.tech.user, organization=tenant_a.org, role="viewer")
    response = _password_login(client, "technician@empresa-b.cl")
    assert not response["Location"].startswith(reverse("sso_login"))


def test_password_reset_only_reaches_admins_on_enforced_domains(client, tenant_a, enforced):
    mail.outbox.clear()
    response = client.post(
        reverse("account_reset_password"), {"email": "technician@empresa-a.cl"}, follow=True
    )
    assert response.status_code == 200
    assert mail.outbox == []
    assert "SSO" in response.content.decode()
    client.post(reverse("account_reset_password"), {"email": "admin@empresa-a.cl"})
    assert [message.to for message in mail.outbox] == [["admin@empresa-a.cl"]]


def test_password_reset_unchanged_without_enforcement(client, tenant_a, idp):
    configure(tenant_a, idp)
    mail.outbox.clear()
    client.post(reverse("account_reset_password"), {"email": "technician@empresa-a.cl"})
    assert [message.to for message in mail.outbox] == [["technician@empresa-a.cl"]]


# --- SSO y MFA (ADR-022) ----------------------------------------------------------------------


def test_sso_counts_as_strong_factor_but_password_admins_need_totp(client, tenant_a, idp, settings):
    settings.MFA_ENFORCE = True
    Organization.objects.filter(pk=tenant_a.org.pk).update(mfa_policy=Organization.MfaPolicy.EVERYONE)
    configure(tenant_a, idp)
    sso_round_trip(client, idp, "technician@empresa-a.cl")
    assert client.get(reverse("dashboard")).status_code == 200
    client.logout()
    login(client, tenant_a.admin)  # contraseña: sin el indicador de SSO en la sesión
    assert client.get(reverse("dashboard"))["Location"] == reverse("mfa_activate_totp")


def test_sso_strong_factor_only_applies_to_the_idp_organization(client, tenant_a, tenant_b, idp, settings):
    settings.MFA_ENFORCE = True
    Organization.objects.filter(pk=tenant_b.org.pk).update(mfa_policy=Organization.MfaPolicy.EVERYONE)
    Membership.objects.create(user=tenant_a.tech.user, organization=tenant_b.org, role="viewer")
    configure(tenant_a, idp)
    sso_round_trip(client, idp, "technician@empresa-a.cl")
    assert client.get(reverse("dashboard")).status_code == 200
    client.post(reverse("switch_organization"), {"organization": str(tenant_b.org.pk)})
    assert client.get(reverse("dashboard"))["Location"] == reverse("mfa_activate_totp")
