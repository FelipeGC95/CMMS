"""Ubicaciones en árbol (ORG-04) y configuración de la organización."""

import zoneinfo

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils.translation import gettext as _

from apps.accounts.permissions import Perm, require_perm
from apps.accounts.scoping import allowed_location_ids
from apps.audit import services as audit
from apps.audit.models import AuditEvent

from .models import Location, Organization
from .selectors import descendant_ids

LOCATION_FIELDS = ["code", "name", "kind", "parent", "timezone", "is_active"]


def _check_scope(membership, *, location=None, parent=None, parent_given=False):
    """Con alcance por sitio solo se administra dentro del propio subárbol, nunca en la raíz."""
    allowed = allowed_location_ids(membership)
    if allowed is None:
        return
    if location is not None and location.pk not in allowed:
        raise ValidationError(_("Esa ubicación está fuera de tu alcance."))
    if parent_given and (parent is None or parent.pk not in allowed):
        raise ValidationError(_("Elige una ubicación padre dentro de tu alcance."))


def _clean(*, code, name, timezone, parent, location=None):
    code, name, timezone = code.strip(), name.strip(), timezone.strip()
    if not code or not name:
        raise ValidationError(_("El código y el nombre son obligatorios."))
    if timezone and timezone not in zoneinfo.available_timezones():
        raise ValidationError(_("Zona horaria no válida (ej. America/Santiago)."))
    duplicate = Location.objects.filter(code__iexact=code)
    if location is not None:
        duplicate = duplicate.exclude(pk=location.pk)
    if duplicate.exists():
        raise ValidationError(_("Ya existe una ubicación con el código %(code)s.") % {"code": code})
    if location is not None and parent is not None:
        if parent.pk == location.pk or parent.pk in descendant_ids(location):
            raise ValidationError(_("Una ubicación no puede quedar dentro de sí misma."))
    return code, name, timezone


@transaction.atomic
def create_location(*, membership, code, name, kind, parent=None, timezone=""):
    require_perm(membership, Perm.ASSET_MANAGE)
    _check_scope(membership, parent=parent, parent_given=True)
    code, name, timezone = _clean(code=code, name=name, timezone=timezone, parent=parent)
    location = Location.objects.create(code=code, name=name, kind=kind, parent=parent, timezone=timezone)
    audit.record(
        action=AuditEvent.Action.CREATE,
        target=location,
        membership=membership,
        changes=audit.snapshot(location, LOCATION_FIELDS),
    )
    return location


@transaction.atomic
def update_location(*, membership, location, code, name, kind, parent, timezone="", is_active=True):
    """Edita, mueve (cambia el padre) o desactiva. Mover arrastra a los descendientes (ORG-04 CA3)."""
    require_perm(membership, Perm.ASSET_MANAGE)
    _check_scope(membership, location=location)
    if parent != location.parent:
        _check_scope(membership, parent=parent, parent_given=True)
    code, name, timezone = _clean(code=code, name=name, timezone=timezone, parent=parent, location=location)
    before = audit.snapshot(location, LOCATION_FIELDS)
    location.code, location.name, location.kind = code, name, kind
    location.parent, location.timezone, location.is_active = parent, timezone, is_active
    location.save()
    changes = audit.diff(before, audit.snapshot(location, LOCATION_FIELDS))
    if changes:
        audit.record(action=AuditEvent.Action.UPDATE, target=location, membership=membership, changes=changes)
    return location


@transaction.atomic
def delete_location(*, membership, location):
    """Solo se elimina si no tiene hijos ni activos; si no, se desactiva (ORG-04 CA2)."""
    require_perm(membership, Perm.ASSET_MANAGE)
    _check_scope(membership, location=location)
    if location.children.exists() or location.assets.exists():
        raise ValidationError(
            _("No se puede eliminar: tiene ubicaciones o activos dentro. Desactívala en su lugar.")
        )
    audit.record(
        action=AuditEvent.Action.DELETE,
        target=location,
        membership=membership,
        changes=audit.snapshot(location, LOCATION_FIELDS),
    )
    location.delete()


# ---------- Configuración de la organización (WO-02 CA2, REQ-03 CA2) ----------

SETTINGS_FIELDS = ["require_close_approval", "require_failure_details"]


@transaction.atomic
def update_organization_settings(*, membership, require_close_approval, require_failure_details):
    """Pasos opcionales del cierre de OT. Solo el administrador de la organización."""
    require_perm(membership, Perm.ORGANIZATION_MANAGE)
    organization = Organization.objects.select_for_update().get(pk=membership.organization_id)
    before = audit.snapshot(organization, SETTINGS_FIELDS)
    organization.require_close_approval = require_close_approval
    organization.require_failure_details = require_failure_details
    changes = audit.diff(before, audit.snapshot(organization, SETTINGS_FIELDS))
    if changes:
        organization.save(update_fields=[*SETTINGS_FIELDS, "updated_at"])
        audit.record(
            action=AuditEvent.Action.UPDATE, target=organization, membership=membership, changes=changes
        )
    return organization
