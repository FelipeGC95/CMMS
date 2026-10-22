"""Verificación en dos pasos (ORG-08): política por organización, configuración forzada y recuperación."""

import pytest
from allauth.account.models import EmailAddress
from allauth.mfa.models import Authenticator
from allauth.mfa.recovery_codes.internal.auth import RecoveryCodes
from allauth.mfa.totp.internal.auth import (
    SECRET_SESSION_KEY,
    TOTP,
    format_hotp_value,
    generate_totp_secret,
    hotp_value,
    yield_hotp_counters_from_time,
)
from django.core.exceptions import ValidationError
from django.urls import reverse

from apps.accounts.mfa import mfa_required
from apps.accounts.models import Membership
from apps.core.tenancy import tenant_context
from apps.organizations.models import Organization
from apps.organizations.services import update_mfa_policy
from tests.api_utils import api_client
from tests.conftest import PASSWORD, login

SETUP_URL = reverse("mfa_activate_totp")


@pytest.fixture(autouse=True)
def _enforce(settings):
    settings.MFA_ENFORCE = True


def set_policy(tenant, policy, membership=None):
    with tenant_context(tenant.org):
        return update_mfa_policy(membership=membership or tenant.admin, mfa_policy=policy)


def current_code(secret):
    return format_hotp_value(hotp_value(secret, next(yield_hotp_counters_from_time())))


def enable_totp(user):
    TOTP.activate(user, generate_totp_secret())
    return RecoveryCodes.activate(user)


def test_default_policy_is_admins_only(tenant_a):
    assert tenant_a.org.mfa_policy == Organization.MfaPolicy.ADMINS_ONLY
    assert mfa_required(tenant_a.admin)
    assert not mfa_required(tenant_a.tech)
    assert not mfa_required(None)


def test_admin_without_mfa_is_sent_to_setup(client, tenant_a):
    login(client, tenant_a.admin)
    response = client.get(reverse("dashboard"))
    assert response.status_code == 302 and response["Location"] == SETUP_URL
    # htmx recibe la redirección en la cabecera que entiende.
    response = client.get(reverse("asset_list"), HTTP_HX_REQUEST="true")
    assert response.status_code == 204 and response["HX-Redirect"] == SETUP_URL


def test_htmx_from_the_setup_page_does_not_reload_it(client, tenant_a):
    """La campana del menú consulta al cargar: redirigir desde la página del QR la recargaba
    cada segundo con un QR nuevo."""
    login(client, tenant_a.admin)
    response = client.get(
        reverse("notification_badge"),
        HTTP_HX_REQUEST="true",
        HTTP_HX_CURRENT_URL=f"https://galbust-cmms.onrender.com{SETUP_URL}",
    )
    assert response.status_code == 204 and "HX-Redirect" not in response


def test_setup_page_and_logout_stay_reachable(client, tenant_a):
    login(client, tenant_a.admin)
    # La activación pide haber ingresado hace poco: se simula con el ingreso por contraseña.
    client.logout()
    EmailAddress.objects.create(user=tenant_a.admin.user, email=tenant_a.admin.user.email, verified=True)
    client.post(reverse("account_login"), {"login": tenant_a.admin.user.email, "password": PASSWORD})
    response = client.get(SETUP_URL)
    assert response.status_code == 200
    assert "exige la verificación en dos pasos" in response.content.decode()
    assert client.get("/static/css/tokens.css").status_code in (200, 404)
    assert client.post(reverse("account_logout")).status_code == 302


def test_forced_setup_then_access(client, tenant_a):
    user = tenant_a.admin.user
    EmailAddress.objects.create(user=user, email=user.email, verified=True)
    client.post(reverse("account_login"), {"login": user.email, "password": PASSWORD})
    assert client.get(reverse("dashboard"))["Location"] == SETUP_URL
    client.get(SETUP_URL)
    secret = client.session[SECRET_SESSION_KEY]
    response = client.post(SETUP_URL, {"code": current_code(secret)})
    assert response.status_code == 302 and response["Location"] == reverse("mfa_view_recovery_codes")
    assert Authenticator.objects.filter(user=user, type=Authenticator.Type.TOTP).exists()
    assert client.get(reverse("dashboard")).status_code == 200


def test_technician_not_forced_under_default_policy(client, tenant_a):
    login(client, tenant_a.tech)
    assert client.get(reverse("dashboard")).status_code == 200


def test_everyone_policy_forces_technicians(client, tenant_a):
    set_policy(tenant_a, Organization.MfaPolicy.EVERYONE)
    login(client, tenant_a.tech)
    assert client.get(reverse("dashboard"))["Location"] == SETUP_URL


def test_optional_policy_forces_nobody(client, tenant_a):
    set_policy(tenant_a, Organization.MfaPolicy.OPTIONAL)
    login(client, tenant_a.admin)
    assert client.get(reverse("dashboard")).status_code == 200


def test_user_with_mfa_enters(client, tenant_a):
    enable_totp(tenant_a.admin.user)
    login(client, tenant_a.admin)
    assert client.get(reverse("dashboard")).status_code == 200


def test_active_organization_policy_applies(client, tenant_a, tenant_b):
    """Técnico en A (sin exigencia) y admin en B (exigida): manda la organización abierta."""
    user = tenant_a.tech.user
    Membership.objects.create(user=user, organization=tenant_b.org, role=Membership.Role.ADMIN)
    login(client, tenant_a.tech)
    client.post(reverse("switch_organization"), {"organization": str(tenant_a.org.pk)})
    assert client.get(reverse("dashboard")).status_code == 200
    client.post(reverse("switch_organization"), {"organization": str(tenant_b.org.pk)})
    assert client.get(reverse("dashboard"))["Location"] == SETUP_URL
    # Volver a A sigue permitido: el cambio de organización está en la lista abierta.
    client.post(reverse("switch_organization"), {"organization": str(tenant_a.org.pk)})
    assert client.get(reverse("dashboard")).status_code == 200


def test_login_with_recovery_code(client, tenant_a):
    user = tenant_a.admin.user
    EmailAddress.objects.create(user=user, email=user.email, verified=True)
    codes = enable_totp(user).get_unused_codes()
    response = client.post(reverse("account_login"), {"login": user.email, "password": PASSWORD})
    assert response["Location"] == reverse("mfa_authenticate")
    assert "_auth_user_id" not in client.session
    page = client.get(reverse("mfa_authenticate"))
    assert "códigos de recuperación" in page.content.decode()
    response = client.post(reverse("mfa_authenticate"), {"code": codes[0]})
    assert response.status_code == 302
    assert "_auth_user_id" in client.session
    assert client.get(reverse("dashboard")).status_code == 200
    # Un código de recuperación sirve una sola vez.
    assert (
        codes[0]
        not in RecoveryCodes(Authenticator.objects.get(user=user, type="recovery_codes")).get_unused_codes()
    )


def test_api_tokens_are_not_affected(tenant_a):
    """La API usa tokens de organización, sin sesión: la política de MFA no aplica."""
    response = api_client(tenant_a).get("/api/v1/assets/")
    assert response.status_code == 200


def test_policy_change_is_admin_only_and_validated(tenant_a):
    from apps.accounts.permissions import PermissionDeniedError

    with pytest.raises(PermissionDeniedError):
        set_policy(tenant_a, Organization.MfaPolicy.OPTIONAL, tenant_a.tech)
    with pytest.raises(ValidationError):
        set_policy(tenant_a, "nunca")


def test_settings_page_saves_policy(client, tenant_a):
    enable_totp(tenant_a.admin.user)
    login(client, tenant_a.admin)
    response = client.post(
        reverse("organization_settings"), {"section": "security", "mfa_policy": "everyone"}
    )
    assert response.status_code == 302
    tenant_a.org.refresh_from_db()
    assert tenant_a.org.mfa_policy == Organization.MfaPolicy.EVERYONE
