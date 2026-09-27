"""Piezas de DRF para la API v1: autenticación por token, alcances, tenant, límites y errores."""

import zoneinfo
from contextlib import ExitStack

from django.utils import timezone
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy
from drf_spectacular.extensions import OpenApiAuthenticationExtension
from rest_framework import exceptions, permissions
from rest_framework.authentication import BaseAuthentication, get_authorization_header
from rest_framework.pagination import PageNumberPagination
from rest_framework.throttling import SimpleRateThrottle

from apps.core.tenancy import tenant_context

from .models import ApiToken
from .services import authenticate_token, principal_for


class OrganizationTokenAuthentication(BaseAuthentication):
    """`Authorization: Bearer <token>`. `request.auth` queda con la membresía de servicio."""

    keyword = b"bearer"

    def authenticate(self, request):
        parts = get_authorization_header(request).split()
        if not parts or parts[0].lower() != self.keyword:
            return None
        if len(parts) != 2:
            raise exceptions.AuthenticationFailed(_("Encabezado de autorización mal formado."))
        token = authenticate_token(parts[1].decode(errors="ignore"))
        if token is None:
            raise exceptions.AuthenticationFailed(_("Token inválido, revocado o vencido."))
        return token.service_user, principal_for(token)

    def authenticate_header(self, request):
        return 'Bearer realm="api"'


class TokenAuthScheme(OpenApiAuthenticationExtension):
    target_class = OrganizationTokenAuthentication
    name = "organizationToken"

    def get_security_definition(self, auto_schema):
        return {"type": "http", "scheme": "bearer", "description": "Token de organización (gbt_…)."}


class HasTokenScope(permissions.BasePermission):
    """Autenticado por token; el alcance "read" solo permite métodos de lectura."""

    message = gettext_lazy("El token no tiene permiso para esta operación.")

    def has_permission(self, request, view):
        principal = request.auth
        token = getattr(principal, "api_token", None)
        if token is None:
            return False
        return request.method in permissions.SAFE_METHODS or token.scope == ApiToken.Scope.WRITE


class TokenRateThrottle(SimpleRateThrottle):
    """Límite de peticiones por token (DEFAULT_THROTTLE_RATES["api_token"])."""

    scope = "api_token"

    def get_cache_key(self, request, view):
        token = getattr(request.auth, "api_token", None)
        if token is None:
            return None  # sin token la petición ya falla por autenticación
        return self.cache_format % {"scope": self.scope, "ident": token.pk}


class Pagination(PageNumberPagination):
    page_size = 50
    page_size_query_param = "page_size"
    max_page_size = 200


class TenantAPIMixin:
    """Fija el contexto de tenant (y la zona horaria) de la organización del token."""

    def dispatch(self, request, *args, **kwargs):
        self._tenant_stack = ExitStack()
        with self._tenant_stack:
            try:
                return super().dispatch(request, *args, **kwargs)
            finally:
                timezone.deactivate()

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        organization = request.auth.organization
        self._tenant_stack.enter_context(tenant_context(organization))
        timezone.activate(zoneinfo.ZoneInfo(organization.timezone))

    @property
    def membership(self):
        return self.request.auth
