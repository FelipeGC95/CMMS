"""Piezas de DRF para la API v1 (token) y la API de terreno (sesión): autenticación, alcances,
tenant, límites y errores."""

import zoneinfo
from contextlib import ExitStack

from django.utils import timezone
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy
from drf_spectacular.extensions import OpenApiAuthenticationExtension
from rest_framework import exceptions, permissions
from rest_framework.authentication import (
    BaseAuthentication,
    SessionAuthentication,
    get_authorization_header,
)
from rest_framework.pagination import PageNumberPagination
from rest_framework.throttling import SimpleRateThrottle, UserRateThrottle

from apps.accounts.models import Membership
from apps.core.tenancy import tenant_context
from apps.inventory.services import InventoryError
from apps.organizations.modules import has_module, plan_message
from apps.work.services import WorkOrderError

from .models import ApiToken
from .services import CONFLICT_CODES, Conflict, authenticate_token, principal_for


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


# --- API de terreno (X-06, ADR-011): sesión de Django bajo /field/api/ ----------

# Versión mínima del cliente de terreno (PWA) que el servidor acepta. Se sube cuando
# un cambio del contrato deja de servir a la versión anterior: el cliente viejo recibe
# 409 `upgrade_required` y se actualiza antes de volver a sincronizar.
MIN_CLIENT_VERSION = 1
CLIENT_VERSION_HEADER = "X-Client-Version"


class MembershipSessionAuthentication(SessionAuthentication):
    """Sesión de Django + CSRF. `request.auth` queda con la membresía real de la persona.

    Es la que dejó `TenantMiddleware` (organización activa de la sesión), con su rol y
    su alcance por sitio: los mismos permisos que la web. Sin membresía → 403; sin
    sesión → 401 (por `authenticate_header`).
    """

    def authenticate(self, request):
        result = super().authenticate(request)
        if result is None:
            return None
        membership = getattr(request._request, "membership", None)
        if membership is None:
            raise exceptions.PermissionDenied(_("No perteneces a ninguna organización activa."))
        membership.user = result[0]  # la misma persona ya cargada: sin otra consulta
        return result[0], membership

    def authenticate_header(self, request):
        return 'Session realm="field"'


class SessionAuthScheme(OpenApiAuthenticationExtension):
    target_class = MembershipSessionAuthentication
    name = "fieldSession"

    def get_security_definition(self, auto_schema):
        return {
            "type": "apiKey",
            "in": "cookie",
            "name": "sessionid",
            "description": "Sesión de la web (solo la PWA de terreno, mismo origen); "
            "las escrituras llevan el encabezado X-CSRFToken.",
        }


class HasMembership(permissions.BasePermission):
    """Persona con una membresía guardada (no un token de servicio)."""

    def has_permission(self, request, view):
        membership = request.auth
        return isinstance(membership, Membership) and membership.pk is not None


class FieldModule(permissions.BasePermission):
    """El plan incluye el módulo del endpoint (`work_orders` o `requests`); no exige `api`."""

    message = gettext_lazy("Tu plan no incluye este módulo.")

    def has_permission(self, request, view):
        organization = request.auth.organization
        module = getattr(view, "field_module", "work_orders")
        if not has_module(organization, module):
            self.message = plan_message(organization, module)
            return False
        return True


class FieldUserThrottle(UserRateThrottle):
    """Límite de peticiones por persona (DEFAULT_THROTTLE_RATES["field_user"])."""

    scope = "field_user"


class UpgradeRequired(exceptions.APIException):
    status_code = 409
    default_code = "upgrade_required"


# Organización con la que el teléfono hizo el cambio en cola. Si la sesión cambió de
# organización (en otra pestaña), el envío se rechaza con 409 `organization_changed` en vez
# de aplicarse en la organización equivocada o terminar como conflicto (ADR-011 §1a).
FIELD_ORGANIZATION_HEADER = "X-Field-Organization"


class OrganizationChanged(exceptions.APIException):
    status_code = 409
    default_code = "organization_changed"


class FieldAPIMixin:
    """Vistas de /field/api/: sesión + membresía real, módulo del plan y límite por persona.

    Los errores de estado de la OT (`WorkOrderError` o `InventoryError` con código `cancelled`, `locked` o
    `conflict`) responden 409 con ese código en vez de 400: el teléfono los muestra como
    conflicto y deja de reintentar. Un cliente con `X-Client-Version` menor que
    `MIN_CLIENT_VERSION` recibe 409 `upgrade_required`.
    """

    authentication_classes = [MembershipSessionAuthentication]
    permission_classes = [HasMembership, FieldModule]
    throttle_classes = [FieldUserThrottle]
    field_module = "work_orders"
    check_client_version = True

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        if self.check_client_version:
            require_client_version(request)
        require_same_organization(request)

    def handle_exception(self, exc):
        if isinstance(exc, WorkOrderError | InventoryError) and exc.code in CONFLICT_CODES:
            exc = Conflict(exc.messages[0], code=exc.code)
        return super().handle_exception(exc)


def require_same_organization(request) -> None:
    expected = request.headers.get(FIELD_ORGANIZATION_HEADER, "").strip()
    if expected and expected != str(request.auth.organization_id):
        raise OrganizationChanged(
            _(
                "Cambiaste de organización: este cambio se envía cuando vuelvas a la "
                "organización donde lo hiciste."
            )
        )


def require_client_version(request) -> None:
    raw = request.headers.get(CLIENT_VERSION_HEADER, "").strip()
    if not raw:
        return
    try:
        version = int(raw)
    except ValueError:
        version = 0
    if version < MIN_CLIENT_VERSION:
        error = UpgradeRequired(_("Hay una versión nueva de la aplicación: actualízala para sincronizar."))
        error.min_client_version = MIN_CLIENT_VERSION
        raise error
