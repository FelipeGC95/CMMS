"""Tokens de API (X-05): alta, revocación y resolución del principal de servicio."""

import datetime
import hashlib
import secrets

from django.db import transaction
from django.utils import timezone

from apps.accounts.models import Membership, User
from apps.accounts.permissions import Perm, require_perm
from apps.audit import services as audit
from apps.audit.models import AuditEvent
from apps.audit.services import API_TOKEN_EMAIL_DOMAIN
from apps.work.models import WorkOrder
from apps.work.services import create_work_order

from .models import ApiToken

TOKEN_PREFIX = "gbt_"
# Rol con que actúa un token: escribe como planificador (activos, lecturas, OT y
# solicitudes), nunca administra equipo, auditoría, configuración ni otros tokens.
# El alcance "read" además limita la API a métodos de lectura (framework.HasTokenScope).
SERVICE_ROLE = Membership.Role.PLANNER
# Actualizar "último uso" como mucho una vez por minuto: evita una escritura por petición.
LAST_USED_RESOLUTION = datetime.timedelta(minutes=1)


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


@transaction.atomic
def create_token(
    *, membership, name: str, scope: str, expires_at: datetime.datetime | None = None
) -> tuple[ApiToken, str]:
    """Crea el token y devuelve (token, valor completo). El valor no se vuelve a mostrar."""
    require_perm(membership, Perm.API_TOKEN_MANAGE)
    prefix = secrets.token_hex(4)
    raw = f"{TOKEN_PREFIX}{prefix}_{secrets.token_urlsafe(32)}"
    service_user = User.objects.create_user(
        f"api-{prefix}{API_TOKEN_EMAIL_DOMAIN}",
        None,  # clave inutilizable: no puede iniciar sesión
        full_name=f"API · {name.strip()}"[:150],
        is_active=False,
    )
    token = ApiToken.objects.create(
        name=name.strip(),
        scope=scope,
        prefix=prefix,
        token_hash=hash_token(raw),
        service_user=service_user,
        created_by=membership.user,
        expires_at=expires_at,
    )
    audit.record(
        action=AuditEvent.Action.CREATE,
        target=token,
        membership=membership,
        changes={"name": token.name, "scope": scope, "prefix": prefix, "expires_at": expires_at},
    )
    return token, raw


@transaction.atomic
def revoke_token(*, membership, token: ApiToken) -> ApiToken:
    require_perm(membership, Perm.API_TOKEN_MANAGE)
    if token.revoked_at is None:
        token.revoked_at = timezone.now()
        token.save(update_fields=["revoked_at", "updated_at"])
        audit.record(
            action=AuditEvent.Action.UPDATE,
            target=token,
            membership=membership,
            changes={"revoked_at": [None, token.revoked_at]},
        )
    return token


def authenticate_token(raw: str) -> ApiToken | None:
    """Token vigente de una organización activa, o None. Marca el último uso."""
    if not raw.startswith(TOKEN_PREFIX):
        return None
    # all_tenants: la organización aún no se conoce; se deduce del token (ADR-003).
    token = (
        ApiToken.all_tenants.select_related("organization", "service_user")
        .filter(token_hash=hash_token(raw), organization__is_active=True)
        .first()
    )
    if token is None or not token.is_usable:
        return None
    now = timezone.now()
    if token.last_used_at is None or now - token.last_used_at >= LAST_USED_RESOLUTION:
        # all_tenants: actualización puntual del token recién resuelto, sin contexto aún.
        ApiToken.all_tenants.filter(pk=token.pk).update(last_used_at=now)
        token.last_used_at = now
    return token


def principal_for(token: ApiToken) -> Membership:
    """Membresía en memoria (no se guarda) con que el token llama a los servicios.

    Toda la organización (sin alcance por sitio) y los permisos de SERVICE_ROLE.
    """
    principal = Membership(
        user=token.service_user,
        organization=token.organization,
        role=SERVICE_ROLE,
        restricted_to_locations=False,
    )
    principal.api_token = token
    return principal


def create_work_order_idempotent(*, membership, client_uuid=None, **data) -> WorkOrder:
    """Alta de OT desde la API: exige gestionar OT y no duplica si `client_uuid` ya existe."""
    require_perm(membership, Perm.WORK_ORDER_MANAGE)
    if client_uuid:
        existing = WorkOrder.objects.filter(client_uuid=client_uuid).first()
        if existing:
            return existing
    return create_work_order(by=membership.user, client_uuid=client_uuid, **data)
