"""Pantallas del SSO (ORG-11): configuración (administradores) e ingreso por el IdP."""

from django.contrib import messages
from django.contrib.auth import login as auth_login
from django.core.paginator import Paginator
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.translation import gettext
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST

from apps.accounts.mfa import SSO_SESSION_KEY
from apps.accounts.permissions import Perm
from apps.core.decorators import membership_required
from apps.core.middleware import SESSION_KEY
from apps.organizations.modules import ModuleNotIncludedError

from . import oidc, selectors, services
from .forms import SsoConfigurationForm, SsoDomainForm, SsoLoginForm, initial_role

FLOW_SESSION_KEY = "sso_flow"


def _redirect_uri(request) -> str:
    return request.build_absolute_uri(reverse("sso_callback"))


# --- Configuración ------------------------------------------------------------------------


def _settings_page(request, *, form=None, domain_form=None, status=200):
    membership = request.membership
    configuration = selectors.get_configuration(membership=membership)
    if form is None:
        initial = {"default_role": "technician"}
        if configuration is not None:
            initial = {
                "discovery_url": configuration.discovery_url,
                "client_id": configuration.client_id,
                "default_role": initial_role(configuration),
                "is_active": configuration.is_active,
                "enforce_sso": configuration.enforce_sso,
            }
        form = SsoConfigurationForm(
            initial=initial,
            organization=membership.organization,
            has_secret=bool(configuration and configuration.has_secret),
        )
    page = Paginator(selectors.domain_list(membership=membership), 25).get_page(request.GET.get("page"))
    context = {
        "configuration": configuration,
        "form": form,
        "domain_form": domain_form or SsoDomainForm(),
        "page": page,
        "has_verified_domain": any(domain.is_verified for domain in page.object_list),
        "redirect_uri": _redirect_uri(request),
        "scopes": oidc.SCOPES,
    }
    return render(request, "sso/settings.html", context, status=status)


@membership_required(perm=Perm.ORGANIZATION_MANAGE)
def sso_settings_view(request):
    if request.method != "POST":
        return _settings_page(request)
    configuration = selectors.get_configuration(membership=request.membership)
    form = SsoConfigurationForm(
        request.POST,
        organization=request.membership.organization,
        has_secret=bool(configuration and configuration.has_secret),
    )
    if form.is_valid():
        try:
            services.save_configuration(membership=request.membership, **form.service_data())
        except services.SsoError as error:
            form.add_error(None, error.message)
        except ModuleNotIncludedError as error:
            form.add_error(None, error.message)
        else:
            messages.success(request, gettext("Configuración SSO guardada."))
            return redirect("sso_settings")
    return _settings_page(request, form=form, status=400)


@require_POST
@membership_required(perm=Perm.ORGANIZATION_MANAGE)
def sso_test_view(request):
    try:
        configuration = services.test_connection(membership=request.membership)
    except services.SsoError as error:
        messages.error(request, error.message)
    else:
        if configuration.last_test_ok:
            messages.success(
                request, gettext("Conexión correcta: el proveedor responde y sus claves se leyeron.")
            )
        else:
            messages.error(request, configuration.last_test_error)
    return redirect("sso_settings")


@require_POST
@membership_required(perm=Perm.ORGANIZATION_MANAGE)
def sso_domain_add_view(request):
    form = SsoDomainForm(request.POST)
    if form.is_valid():
        try:
            domain = services.add_domain(membership=request.membership, domain=form.cleaned_data["domain"])
        except services.SsoError as error:
            form.add_error("domain", error.message)
        else:
            messages.success(
                request,
                gettext("Dominio %(domain)s agregado. Crea el registro TXT y luego pulsa «Verificar».")
                % {"domain": domain.domain},
            )
            return redirect(f"{reverse('sso_settings')}#domains")
    return _settings_page(request, domain_form=form, status=400)


@require_POST
@membership_required(perm=Perm.ORGANIZATION_MANAGE)
def sso_domain_verify_view(request, pk):
    domain = selectors.get_domain(membership=request.membership, pk=pk)
    try:
        services.verify_domain(membership=request.membership, domain=domain)
    except services.SsoError as error:
        messages.error(request, error.message)
    else:
        messages.success(request, gettext("Dominio %(domain)s verificado.") % {"domain": domain.domain})
    return redirect(f"{reverse('sso_settings')}#domains")


@require_POST
@membership_required(perm=Perm.ORGANIZATION_MANAGE)
def sso_domain_remove_view(request, pk):
    domain = selectors.get_domain(membership=request.membership, pk=pk)
    services.remove_domain(membership=request.membership, domain=domain)
    messages.success(request, gettext("Dominio %(domain)s quitado.") % {"domain": domain.domain})
    return redirect(f"{reverse('sso_settings')}#domains")


# --- Ingreso ------------------------------------------------------------------------------


def _safe_next(request, value: str) -> str:
    if value and url_has_allowed_host_and_scheme(
        value, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        return value
    return ""


@never_cache
def sso_login_view(request):
    """«Entrar con SSO»: pide el correo y redirige al IdP de la organización dueña del dominio."""
    if request.user.is_authenticated:
        return redirect("dashboard")
    form = SsoLoginForm(request.POST or None, initial={"email": request.GET.get("email", "")})
    if request.method == "POST" and form.is_valid():
        try:
            url, flow = services.begin_login(
                email=form.cleaned_data["email"],
                redirect_uri=_redirect_uri(request),
                next_url=_safe_next(request, request.POST.get("next", "")),
            )
        except services.SsoError as error:
            form.add_error("email", error.message)
        else:
            request.session[FLOW_SESSION_KEY] = flow
            return redirect(url)
    return render(
        request,
        "sso/login.html",
        {"form": form, "next": _safe_next(request, request.GET.get("next", ""))},
        status=400 if form.errors else 200,
    )


@never_cache
def sso_callback_view(request):
    """Vuelta desde el IdP: valida y abre la sesión en la organización del dominio."""
    flow = request.session.pop(FLOW_SESSION_KEY, None)  # un solo uso
    try:
        result = services.complete_login(flow=flow, params=request.GET, redirect_uri=_redirect_uri(request))
    except services.SsoError as error:
        form = SsoLoginForm(initial={"email": (flow or {}).get("email", "")})
        return render(request, "sso/login.html", {"form": form, "sso_error": error.message}, status=400)
    # El SSO cuenta como factor fuerte para esta organización (ADR-022 / ADR-032): login de
    # Django sin las etapas de allauth (no pide el código TOTP).
    auth_login(request, result.user, backend="django.contrib.auth.backends.ModelBackend")
    request.session[SESSION_KEY] = str(result.organization.pk)
    request.session[SSO_SESSION_KEY] = str(result.organization.pk)
    if result.provisioned:
        messages.success(
            request,
            gettext("Bienvenido a %(organization)s. Tu acceso se creó con el inicio de sesión único.")
            % {"organization": result.organization.name},
        )
    return redirect(_safe_next(request, flow.get("next", "")) or "dashboard")
