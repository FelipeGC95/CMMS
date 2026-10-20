"""Estado de acceso por facturación y solo lectura central (ORG-13 CA5, ADR-033).

`billing_status(organization)` se calcula solo con campos de la organización (ya cargada por
`TenantMiddleware`): ninguna consulta extra por petición.

Solo lectura = se ve y se exporta, no se crea ni se edita. Se aplica en un solo lugar por canal:
- web y `/field/api/` (sesión): `ReadOnlyMiddleware` bloquea los métodos no seguros salvo la lista
  blanca `WRITABLE_PREFIXES`, con 402 y un mensaje claro (JSON en `/field/api/`);
- API v1 (token): el permiso `OrganizationWritable` responde 402 `payment_required`.
Rige por impago (gracia vencida) y durante el borrado programado (X-04, cierra el pendiente de ADR-027).
"""

import datetime
from dataclasses import dataclass
from enum import StrEnum

from django.conf import settings
from django.contrib import messages
from django.http import HttpResponse, JsonResponse
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _
from rest_framework import exceptions, permissions

from apps.accounts.permissions import Perm, has_perm, organization_of

TRIAL_WARNING_DAYS = 7


class Access(StrEnum):
    EXEMPT = "exempt"  # facturación manual: sin bloqueo
    TRIAL = "trial"
    ACTIVE = "active"
    GRACE = "grace"  # prueba vencida o pago rechazado: aún se escribe, con aviso
    READ_ONLY = "read_only"


class Reason(StrEnum):
    NONE = ""
    TRIAL_ENDED = "trial_ended"
    PAYMENT_FAILED = "payment_failed"
    UNPAID = "unpaid"  # período pagado terminado sin renovar (p. ej. tras cancelar)
    DELETION = "deletion"


@dataclass(frozen=True)
class BillingStatus:
    access: Access
    reason: Reason = Reason.NONE
    # Fin de la prueba (TRIAL), del período pagado (ACTIVE) o de la gracia (GRACE).
    ends_at: datetime.datetime | None = None

    @property
    def read_only(self) -> bool:
        return self.access == Access.READ_ONLY

    @property
    def days_left(self) -> int | None:
        if self.ends_at is None:
            return None
        return max(0, (self.ends_at - timezone.now()).days)

    @property
    def trial_ending(self) -> bool:
        return (
            self.access == Access.TRIAL
            and self.ends_at is not None
            and self.ends_at - timezone.now() <= datetime.timedelta(days=TRIAL_WARNING_DAYS)
        )

    @property
    def needs_attention(self) -> bool:
        """Para el aviso a administradores: prueba por vencer, gracia o solo lectura."""
        return self.trial_ending or self.access in (Access.GRACE, Access.READ_ONLY)


def grace_period() -> datetime.timedelta:
    return datetime.timedelta(days=settings.BILLING_GRACE_DAYS)


def billing_status(organization, *, now=None) -> BillingStatus:
    """Estado de acceso de la organización. Solo lee sus campos (sin consultas)."""
    from apps.organizations.models import Organization

    now = now or timezone.now()
    if organization.status == Organization.Status.PENDING_DELETION:
        return BillingStatus(Access.READ_ONLY, Reason.DELETION, organization.deletion_scheduled_for)
    if organization.billing_exempt:
        return BillingStatus(Access.EXEMPT)
    trial_end, paid_through = organization.trial_ends_at, organization.paid_through
    covered = max([moment for moment in (trial_end, paid_through) if moment], default=None)
    if organization.past_due_since:
        grace_end = max(organization.past_due_since, covered or organization.past_due_since) + grace_period()
        if now < grace_end:
            return BillingStatus(Access.GRACE, Reason.PAYMENT_FAILED, grace_end)
        return BillingStatus(Access.READ_ONLY, Reason.PAYMENT_FAILED)
    if paid_through and paid_through > now:
        return BillingStatus(Access.ACTIVE, Reason.NONE, paid_through)
    if trial_end and trial_end > now:
        return BillingStatus(Access.TRIAL, Reason.NONE, trial_end)
    reason = Reason.UNPAID if paid_through else Reason.TRIAL_ENDED
    grace_end = (covered or organization.created_at or now) + grace_period()
    if now < grace_end:
        return BillingStatus(Access.GRACE, reason, grace_end)
    return BillingStatus(Access.READ_ONLY, reason)


def is_read_only(organization) -> bool:
    return organization is not None and billing_status(organization).read_only


# Interfaz coherente con la solo lectura: estos permisos solo sirven para crear, editar o
# ejecutar, así que en solo lectura la interfaz los da por no concedidos y oculta sus botones,
# formularios y accesos. Los de consulta (ver inventario, costos, condición, auditoría,
# conversaciones de todos) y `organization.manage` (Facturación, exportar, cancelar el borrado)
# siguen vigentes. Los servicios no cambian: el bloqueo real es `ReadOnlyMiddleware`.
WRITE_PERMS: frozenset[Perm] = frozenset(
    {
        Perm.ASSET_MANAGE,
        Perm.PLAN_MANAGE,
        Perm.WORK_ORDER_MANAGE,
        Perm.WORK_ORDER_EXECUTE,
        Perm.REQUEST_CREATE,
        Perm.METER_READING_CREATE,
        Perm.TEAM_MANAGE,
        Perm.API_TOKEN_MANAGE,
        Perm.INVENTORY_MOVE,
        Perm.INVENTORY_MANAGE,
        Perm.CONDITION_MANAGE,
        Perm.ASSISTANT_USE,
    }
)


def can_act(membership, perm: Perm) -> bool:
    """Permiso para mostrar una acción: `has_perm` y, si es de escritura, que no haya solo lectura.

    Para botones y formularios de la interfaz. Para la navegación a una página que se puede ver
    (listas, fichas, configuración en modo consulta) se usa `has_perm`. Sin consultas extra: la
    organización sale del contexto.
    """
    if not has_perm(membership, perm):
        return False
    return perm not in WRITE_PERMS or not is_read_only(organization_of(membership))


def read_only_message(status: BillingStatus) -> str:
    if status.reason == Reason.DELETION:
        return _(
            "La organización tiene la eliminación programada y está en solo lectura: puedes ver y "
            "exportar tus datos. Un administrador puede cancelar la eliminación en Configuración."
        )
    return _(
        "La organización está en solo lectura por falta de pago: puedes ver y exportar tus datos, "
        "pero no crear ni editar. Un administrador puede pagar en Configuración → Facturación y se "
        "reactiva sola."
    )


# Escrituras permitidas en solo lectura: facturación (para pagar), webhooks entrantes, cuenta
# personal (salir, contraseña, MFA), cambio de organización, exportaciones, borrado (y su
# cancelación), notificaciones propias, impresión de etiquetas y el admin de plataforma.
WRITABLE_PREFIXES = (
    "/settings/billing/",
    "/billing/webhooks/",
    "/accounts/",
    "/org/switch/",
    "/settings/export/",
    "/settings/delete/",
    "/notifications/",
    "/account/notifications/",
    "/assets/labels/",
    "/admin/",
)
# La API v1 (token) la cubre `OrganizationWritable`; el webhook entrante no lleva sesión.
SKIPPED_PREFIXES = ("/api/",)
SAFE_METHODS = ("GET", "HEAD", "OPTIONS", "TRACE")


def is_writable_path(path: str) -> bool:
    return path.startswith(WRITABLE_PREFIXES) or path.startswith(SKIPPED_PREFIXES)


class ReadOnlyMiddleware:
    """Deja `request.billing_status` y bloquea escrituras de organizaciones en solo lectura."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        organization = getattr(request, "organization", None)
        request.billing_status = billing_status(organization) if organization is not None else None
        status = request.billing_status
        if status is None or not status.read_only or request.method in SAFE_METHODS:
            return self.get_response(request)
        if is_writable_path(request.path):
            return self.get_response(request)
        return self._blocked(request, status)

    @staticmethod
    def _blocked(request, status):
        message = read_only_message(status)
        if request.path.startswith("/field/api/"):
            return JsonResponse(
                {"error": {"code": "payment_required", "message": message, "details": {}}}, status=402
            )
        membership = getattr(request, "membership", None)
        is_admin = has_perm(membership, Perm.ORGANIZATION_MANAGE)
        if request.headers.get("HX-Request"):
            # htmx no muestra respuestas de error: vuelve a la página con el mensaje.
            messages.error(request, message)
            response = HttpResponse(status=402)
            response["HX-Redirect"] = request.headers.get("HX-Current-URL") or reverse("dashboard")
            return response
        return render(
            request,
            "billing/read_only.html",
            {"message": message, "is_admin": is_admin, "status": status},
            status=402,
        )


class PaymentRequired(exceptions.APIException):
    status_code = 402
    default_code = "payment_required"


class OrganizationWritable(permissions.BasePermission):
    """API v1: la organización del token en solo lectura no escribe (402 `payment_required`)."""

    def has_permission(self, request, view):
        if request.method in permissions.SAFE_METHODS:
            return True
        organization = getattr(request.auth, "organization", None)
        if organization is None:
            return True  # sin token la autenticación ya responde 401
        status = billing_status(organization)
        if status.read_only:
            raise PaymentRequired(read_only_message(status))
        return True
