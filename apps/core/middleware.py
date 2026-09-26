import zoneinfo

from django.conf import settings
from django.utils import timezone

from .tenancy import tenant_context

SESSION_KEY = "active_organization_id"


class TenantMiddleware:
    """Resuelve la organización activa desde la sesión (ADR-003).

    Deja `request.organization`, `request.membership` y `request.memberships`,
    activa la zona horaria de la organización y fija el contexto de tenant.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.organization = None
        request.membership = None
        request.memberships = []

        if request.user.is_authenticated:
            from apps.accounts.models import Membership

            memberships = list(
                Membership.objects.select_related("organization").filter(
                    user=request.user, is_active=True, organization__is_active=True
                )
            )
            request.memberships = memberships
            if memberships:
                wanted = request.session.get(SESSION_KEY)
                if wanted is None and request.user.last_organization_id:
                    # Sesión nueva: se abre la última organización usada (ORG-07).
                    wanted = str(request.user.last_organization_id)
                membership = next(
                    (m for m in memberships if str(m.organization_id) == wanted),
                    memberships[0],
                )
                request.membership = membership
                request.organization = membership.organization
                request.session[SESSION_KEY] = str(membership.organization_id)

        if request.organization is None:
            timezone.deactivate()
            return self.get_response(request)

        timezone.activate(zoneinfo.ZoneInfo(request.organization.timezone))
        try:
            with tenant_context(request.organization):
                return self.get_response(request)
        finally:
            timezone.deactivate()


class PrivateCacheMiddleware:
    """Las páginas con sesión no se guardan en el caché del navegador (ORG-07 CA2).

    Tras cambiar de organización o cerrar sesión, "Atrás" no debe mostrar datos
    de la organización anterior. Se excluyen estáticos, media, /healthz y las
    respuestas a visitantes sin sesión.
    """

    SKIPPED_PATHS = ("/healthz",)

    def __init__(self, get_response):
        self.get_response = get_response
        self.skipped_prefixes = tuple(
            prefix for prefix in (settings.STATIC_URL, settings.MEDIA_URL) if prefix
        )

    def __call__(self, request):
        response = self.get_response(request)
        user = getattr(request, "user", None)
        if user is None or not user.is_authenticated:
            return response
        path = request.path
        if path in self.SKIPPED_PATHS or path.startswith(self.skipped_prefixes):
            return response
        # Reemplaza cualquier directiva previa: con sesión nada se comparte ni se guarda.
        response["Cache-Control"] = "no-store, private"
        return response
