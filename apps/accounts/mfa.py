"""Verificación en dos pasos obligatoria según la organización activa (ORG-08, ADR-022).

La política vive en `Organization.mfa_policy`. Un usuario con varias organizaciones
cumple la de la organización que tiene abierta: al cambiar a otra, se evalúa la nueva.
"""

from allauth.mfa.models import Authenticator
from allauth.mfa.utils import is_mfa_enabled
from django.conf import settings
from django.http import HttpResponse, JsonResponse
from django.shortcuts import redirect
from django.urls import reverse
from django.utils.translation import gettext as _

from apps.organizations.models import Organization

from .models import Membership

# Rutas que siguen abiertas mientras falta configurar el segundo factor: las de allauth
# (configuración, ingreso, salida, correo), estáticos, salud, la API por token (no usa
# sesión) y el cambio de organización (la política es la de la organización activa).
ALLOWED_PREFIXES = (
    "/accounts/",
    "/static/",
    "/media/",
    "/api/",
    "/healthz",
    "/readyz",
    "/org/switch/",
    "/org-theme.css",
    # PWA de terreno (ADR-011): el navegador los pide solo, sin datos. /field/ y /field/api/ no.
    "/field/sw.js",
    "/field/manifest.webmanifest",
)
# La API de terreno no redirige: el teléfono recibe 403 `mfa_required` y pausa la cola.
JSON_PREFIXES = ("/field/api/",)


def mfa_required(membership) -> bool:
    """¿La organización de esta membresía le exige segundo factor?"""
    if membership is None:
        return False
    policy = membership.organization.mfa_policy
    if policy == Organization.MfaPolicy.EVERYONE:
        return True
    if policy == Organization.MfaPolicy.ADMINS_ONLY:
        return membership.role == Membership.Role.ADMIN
    return False


class MFARequiredMiddleware:
    """Tras iniciar sesión, quien debe tener segundo factor y no lo tiene va a configurarlo.

    Va después de TenantMiddleware (necesita `request.membership`). `MFA_ENFORCE=0`
    lo apaga (tests que no tratan de MFA).
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if self._must_set_up(request):
            url = reverse("mfa_activate_totp")
            if request.path.startswith(JSON_PREFIXES):
                message = _("Configura la verificación en dos pasos para seguir sincronizando.")
                error = {"code": "mfa_required", "message": message, "details": {"setup_url": url}}
                return JsonResponse({"error": error}, status=403)
            if request.headers.get("HX-Request"):
                response = HttpResponse(status=204)
                response["HX-Redirect"] = url
                return response
            return redirect(url)
        return self.get_response(request)

    @staticmethod
    def _must_set_up(request) -> bool:
        if not getattr(settings, "MFA_ENFORCE", True):
            return False
        if not request.user.is_authenticated or request.path.startswith(ALLOWED_PREFIXES):
            return False
        if not mfa_required(getattr(request, "membership", None)):
            return False
        return not is_mfa_enabled(request.user, [Authenticator.Type.TOTP])
