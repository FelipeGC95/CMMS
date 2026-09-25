"""Registro de eventos de auditoría (X-01).

`record` no abre transacción propia: se llama dentro del `transaction.atomic`
del servicio que audita, así el evento se revierte junto con el cambio.
"""

from django.contrib.contenttypes.models import ContentType

from apps.core.tenancy import CrossTenantError, require_current_organization

from .context import RequestOrigin, get_request_origin
from .models import AuditEvent

# Campos que nunca se guardan en `changes`, aunque cambien.
SENSITIVE_FIELD_PARTS = ("password", "secret", "token", "key")
# Campos de sistema que no aportan a un diff.
SKIPPED_FIELDS = {"id", "organization", "created_at", "updated_at"}


def is_sensitive(field_name: str) -> bool:
    name = field_name.lower()
    return any(part in name for part in SENSITIVE_FIELD_PARTS)


def _clean(changes: dict) -> dict:
    return {
        key: _clean(value) if isinstance(value, dict) else value
        for key, value in changes.items()
        if not is_sensitive(str(key))
    }


def snapshot(instance, fields=None) -> dict:
    """Valores de los campos concretos de `instance` (las FK como ID)."""
    return {
        field.attname: field.value_from_object(instance)
        for field in instance._meta.concrete_fields
        if field.name not in SKIPPED_FIELDS and (fields is None or field.name in fields)
    }


def diff(before: dict, after: dict) -> dict:
    """{campo: [antes, después]} solo para los campos que cambiaron."""
    return {
        key: [before.get(key), after.get(key)]
        for key in before.keys() | after.keys()
        if before.get(key) != after.get(key)
    }


def record(
    *,
    action: str,
    target,
    changes: dict | None = None,
    membership=None,
    actor=None,
    request=None,
) -> AuditEvent:
    """Registra un evento en la organización del contexto (falla sin contexto)."""
    organization = require_current_organization()
    target_org_id = getattr(target, "organization_id", None)
    if target_org_id is not None and target_org_id != organization.id:
        raise CrossTenantError("El objeto auditado pertenece a otra organización.")

    if actor is None and membership is not None:
        actor = membership.user
    if request is not None:
        origin = RequestOrigin.from_request(request)
        actor = actor or (request.user if request.user.is_authenticated else None)
    else:
        origin = get_request_origin() or RequestOrigin(ip=None, user_agent="")

    return AuditEvent.objects.create(
        actor=actor,
        actor_type=AuditEvent.ActorType.USER if actor else AuditEvent.ActorType.SYSTEM,
        actor_repr=(actor.email if actor else "")[:254],
        action=action,
        target_type=ContentType.objects.get_for_model(target),
        target_id=str(target.pk),
        target_repr=str(target)[:200],
        changes=_clean(changes or {}),
        ip=origin.ip,
        user_agent=origin.user_agent,
        request_id=origin.request_id,
    )
