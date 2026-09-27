import zoneinfo

from django.conf import settings
from django.http import HttpResponseRedirect
from django.utils import timezone

from .tenancy import tenant_context

SESSION_KEY = "active_organization_id"
ORG_PARAM = "org"


def is_top_level_navigation(request) -> bool:
    """GET de navegación de primer nivel (un clic en un enlace o la barra de direcciones).

    Con Fetch Metadata se descartan imágenes, fetch, iframes y htmx: un sitio ajeno
    no puede cambiar la organización activa en segundo plano mientras se trabaja en
    otra pestaña. Navegadores sin esas cabeceras se aceptan (solo GET).
    """
    if request.method != "GET" or request.headers.get("HX-Request"):
        return False
    dest = request.headers.get("Sec-Fetch-Dest")
    mode = request.headers.get("Sec-Fetch-Mode")
    return dest in (None, "document") and mode in (None, "navigate")


def url_without_org(request) -> str:
    query = request.GET.copy()
    query.pop(ORG_PARAM, None)
    return request.path + (f"?{query.urlencode()}" if query else "")


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
                Membership.objects.select_related("organization__plan")
                .filter(user=request.user, is_active=True, organization__is_active=True)
                # Orden estable: sin última organización guardada se abre siempre la misma.
                .order_by("organization__name", "organization_id")
            )
            request.memberships = memberships
            if ORG_PARAM in request.GET and is_top_level_navigation(request):
                return self._open_link_in_organization(request)
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

    @staticmethod
    def _open_link_in_organization(request):
        """Enlaces de correo con `?org=<id>` (ADR-003): abren en esa organización.

        Solo cambia si la persona tiene membresía activa ahí (el mismo servicio del
        selector: rota la sesión y la recuerda). Sin membresía se ignora. En ambos
        casos se redirige a la misma URL sin el parámetro, así la respuesta no revela
        si la organización existe ni queda en marcadores o en el historial.
        """
        from apps.accounts.services import switch_organization

        wanted = request.GET[ORG_PARAM]
        if request.session.get(SESSION_KEY) != wanted:
            switch_organization(request=request, organization_id=wanted)
        return HttpResponseRedirect(url_without_org(request))


class ContentSecurityPolicyMiddleware:
    """Agrega la cabecera Content-Security-Policy (ADR-023) a toda respuesta que no la traiga."""

    def __init__(self, get_response):
        self.get_response = get_response
        self.policy = getattr(settings, "CONTENT_SECURITY_POLICY", "")

    def __call__(self, request):
        response = self.get_response(request)
        if self.policy and "Content-Security-Policy" not in response:
            response["Content-Security-Policy"] = self.policy
        return response


class PrivateCacheMiddleware:
    """Las páginas con sesión no se guardan en el caché del navegador (ORG-07 CA2).

    Tras cambiar de organización o cerrar sesión, "Atrás" no debe mostrar datos
    de la organización anterior. Se excluyen estáticos, media, /healthz, /readyz y las
    respuestas a visitantes sin sesión.
    """

    # /org-theme.css no tiene datos: solo los colores de la marca (ORG-09), cacheables.
    SKIPPED_PATHS = ("/healthz", "/readyz", "/org-theme.css")

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
