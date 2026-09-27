"""Ubicaciones en árbol (ORG-04) y configuración de la organización."""

import datetime
import io
import logging
import zoneinfo

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.files import File
from django.core.files.base import ContentFile
from django.db import transaction
from django.template.defaultfilters import filesizeformat
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy
from PIL import Image, ImageOps, UnidentifiedImageError

from apps.accounts.permissions import Perm, require_perm
from apps.accounts.scoping import allowed_location_ids
from apps.attachments.selectors import storage_usage
from apps.audit import services as audit
from apps.audit.models import AuditEvent
from apps.core.tenancy import tenant_context

from . import exports
from .branding import brand_palette
from .models import DataExport, Location, Organization
from .selectors import descendant_ids

logger = logging.getLogger(__name__)

DELETION_FIELDS = ["status", "deletion_requested_at", "deletion_scheduled_for", "deletion_requested_by"]
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


# ---------- Cupos del plan (ORG-10 CA1) ----------

QUOTA_WARNING_RATIO = 0.8


class QuotaExceeded(ValidationError):
    """Crear más superaría el cupo del plan. Leer y editar lo existente sigue permitido."""


def _quota(organization, resource):
    """(en uso, máximo) del recurso; máximo None = ilimitado. Cuenta en la organización activa."""
    plan = organization.plan
    if resource == "assets":
        from apps.assets.models import Asset  # diferido: assets depende de organizations

        # Cuentan todos los activos, también los dados de baja (igual que en Configuración).
        return Asset.objects.filter(organization=organization).count(), plan.max_assets if plan else None
    raise ValueError(f"Cupo desconocido: {resource}")


QUOTA_LABELS = {"assets": gettext_lazy("activos")}


def check_quota(*, organization, resource, adding=1):
    """Bloquea la creación si superaría el cupo (100 %). Llamar dentro de la transacción que crea.

    Bloquea la fila de la organización para que dos altas simultáneas no pasen ambas el límite.
    """
    Organization.objects.select_for_update().filter(pk=organization.pk).first()
    used, limit = _quota(organization, resource)
    if limit is not None and used + adding > limit:
        raise QuotaExceeded(
            _(
                "Tu plan %(plan)s permite hasta %(limit)s %(resource)s y ya tienes %(used)s. "
                "Puedes seguir viendo y editando, pero para crear más hay que cambiar de plan."
            )
            % {
                "plan": organization.plan.name,
                "limit": limit,
                "resource": QUOTA_LABELS[resource],
                "used": used,
            }
        )


def quota_warning(*, organization, resource):
    """Aviso al llegar al 80 % del cupo; None si no hay nada que avisar."""
    used, limit = _quota(organization, resource)
    if not limit or used < limit * QUOTA_WARNING_RATIO:
        return None
    return _("Usas %(used)s de %(limit)s %(resource)s de tu plan (%(percent)s %%).") % {
        "used": used,
        "limit": limit,
        "resource": QUOTA_LABELS[resource],
        "percent": min(100, round(used * 100 / limit)),
    }


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


@transaction.atomic
def set_anonymous_reports(*, membership, allow_anonymous_reports: bool):
    """Enciende o apaga el aviso por QR sin cuenta (REQ-02 CA1). Solo el administrador."""
    require_perm(membership, Perm.ORGANIZATION_MANAGE)
    organization = Organization.objects.select_for_update().get(pk=membership.organization_id)
    before = organization.allow_anonymous_reports
    if before != allow_anonymous_reports:
        organization.allow_anonymous_reports = allow_anonymous_reports
        organization.save(update_fields=["allow_anonymous_reports", "updated_at"])
        audit.record(
            action=AuditEvent.Action.UPDATE,
            target=organization,
            membership=membership,
            changes={"allow_anonymous_reports": [before, allow_anonymous_reports]},
        )
    return organization


@transaction.atomic
def update_mfa_policy(*, membership, mfa_policy):
    """Quién debe usar verificación en dos pasos en la organización (ORG-08)."""
    require_perm(membership, Perm.ORGANIZATION_MANAGE)
    if mfa_policy not in Organization.MfaPolicy.values:
        raise ValidationError(_("Política de verificación en dos pasos no válida."))
    organization = Organization.objects.select_for_update().get(pk=membership.organization_id)
    before = organization.mfa_policy
    if before != mfa_policy:
        organization.mfa_policy = mfa_policy
        organization.save(update_fields=["mfa_policy", "updated_at"])
        audit.record(
            action=AuditEvent.Action.UPDATE,
            target=organization,
            membership=membership,
            changes={"mfa_policy": [before, mfa_policy]},
        )
    return organization


@transaction.atomic
def set_primary_color(*, membership, color):
    """Color primario de la organización (ORG-09). En blanco vuelve al de GALBUST.

    Devuelve la paleta ajustada: sus `notes` informan los tonos cambiados por contraste (CA1).
    """
    require_perm(membership, Perm.ORGANIZATION_MANAGE)
    palette = None
    if color:
        try:
            palette = brand_palette(color)
        except ValueError as error:
            raise ValidationError(_("Elige un color en formato #RRGGBB.")) from error
        color = palette.color
    organization = Organization.objects.select_for_update().get(pk=membership.organization_id)
    before = organization.primary_color
    if before != color:
        organization.primary_color = color
        organization.save(update_fields=["primary_color", "updated_at"])
        audit.record(
            action=AuditEvent.Action.UPDATE,
            target=organization,
            membership=membership,
            changes={"primary_color": [before, color]},
        )
    return palette


# ---------- Nombres visibles de los estados de OT (WO-02 CA5) ----------

STATUS_LABEL_MAX_LENGTH = 40


def work_order_status_choices():
    """[(estado, nombre por defecto traducido)] en el orden del ciclo de vida."""
    # Import diferido: los modelos de `organizations` no dependen de `work`.
    from apps.work.models import WorkOrder

    return list(WorkOrder.Status.choices)


@transaction.atomic
def update_work_order_status_labels(*, membership, labels):
    """Guarda nombres propios para los estados de OT. En blanco vuelve al nombre por defecto.

    `labels` es un dict {estado: nombre}; solo se aceptan estados de `WorkOrder.Status`.
    """
    require_perm(membership, Perm.ORGANIZATION_MANAGE)
    known = [key for key, _label in work_order_status_choices()]
    unknown = sorted(set(labels) - set(known))
    if unknown:
        raise ValidationError(_("Estado de OT desconocido: %(keys)s.") % {"keys": ", ".join(unknown)})
    cleaned = {}
    for key in known:
        value = labels.get(key) or ""
        if not isinstance(value, str):
            raise ValidationError(_("El nombre de un estado debe ser texto."))
        value = " ".join(value.split())
        if len(value) > STATUS_LABEL_MAX_LENGTH:
            raise ValidationError(
                _("El nombre de un estado admite hasta %(max)s caracteres.")
                % {"max": STATUS_LABEL_MAX_LENGTH}
            )
        if value:
            cleaned[key] = value
    organization = Organization.objects.select_for_update().get(pk=membership.organization_id)
    before = organization.work_order_status_labels or {}
    changes = {
        f"work_order_status_labels.{key}": [before.get(key, ""), cleaned.get(key, "")]
        for key in known
        if before.get(key, "") != cleaned.get(key, "")
    }
    if changes:
        organization.work_order_status_labels = cleaned
        organization.save(update_fields=["work_order_status_labels", "updated_at"])
        audit.record(
            action=AuditEvent.Action.UPDATE, target=organization, membership=membership, changes=changes
        )
    return organization


# ---------- Logo de la organización (ORG-09, parte mínima; AST-05 CA2) ----------

LOGO_MAX_BYTES = 1024 * 1024
LOGO_MAX_DIMENSION = 600  # px del lado mayor: sobra para una etiqueta impresa a 300 ppp.
LOGO_MAX_PIXELS = 40_000_000  # antes de decodificar: evita imágenes "bomba" dentro de 1 MB.
LOGO_FORMATS = {"PNG", "JPEG", "WEBP"}


def _normalize_logo(uploaded_file) -> bytes:
    """Valida el logo y lo guarda como PNG de ≤600 px (conserva la transparencia, sin metadatos)."""
    if uploaded_file.size > LOGO_MAX_BYTES:
        raise ValidationError(_("El logo debe pesar hasta 1 MB."))
    invalid = ValidationError(_("El logo debe ser una imagen PNG, JPEG o WebP."))
    try:
        image = Image.open(uploaded_file)
        if image.format not in LOGO_FORMATS:
            raise invalid
        width, height = image.size
        if width * height > LOGO_MAX_PIXELS:
            raise ValidationError(_("El logo tiene demasiados píxeles; redúcelo e inténtalo de nuevo."))
        image.load()
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError) as error:
        raise invalid from error
    image = ImageOps.exif_transpose(image)
    image = image.convert("RGBA" if "A" in image.getbands() or "transparency" in image.info else "RGB")
    image.thumbnail((LOGO_MAX_DIMENSION, LOGO_MAX_DIMENSION), Image.LANCZOS)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def _replace_logo(organization, *, data: bytes | None):
    """Cambia el archivo del logo; el anterior se borra del almacenamiento tras el commit."""
    old_name = organization.logo.name
    if data is None:
        organization.logo = ""
        organization.logo_size_bytes = 0
    else:
        organization.logo.save("logo.png", ContentFile(data), save=False)
        organization.logo_size_bytes = len(data)
    organization.save(update_fields=["logo", "logo_size_bytes", "updated_at"])
    if old_name:
        storage = organization.logo.storage
        transaction.on_commit(lambda: storage.delete(old_name))


@transaction.atomic
def set_organization_logo(*, membership, uploaded_file):
    """Sube o reemplaza el logo. Solo el administrador de la organización.

    Cuenta para el cupo de almacenamiento (ORG-10): se mide el PNG ya normalizado y, al
    reemplazarlo, se descuenta el logo anterior. La fila bloqueada evita que una subida
    simultánea de adjuntos supere el cupo entre ambas.
    """
    require_perm(membership, Perm.ORGANIZATION_MANAGE)
    data = _normalize_logo(uploaded_file)
    organization = Organization.objects.select_for_update().get(pk=membership.organization_id)
    usage = storage_usage(organization=organization)
    if not usage.allows(len(data) - organization.logo_size_bytes):
        raise ValidationError(
            _(
                "No hay espacio para este logo (%(size)s): la organización usa %(used)s de "
                "%(quota)s de su plan. Pide al administrador ampliar el plan."
            )
            % {
                "size": filesizeformat(len(data)),
                "used": filesizeformat(usage.used_bytes),
                "quota": filesizeformat(usage.quota_bytes),
            }
        )
    before = organization.logo.name
    _replace_logo(organization, data=data)
    audit.record(
        action=AuditEvent.Action.UPDATE,
        target=organization,
        membership=membership,
        changes={"logo": [before, organization.logo.name]},
    )
    return organization


@transaction.atomic
def remove_organization_logo(*, membership):
    require_perm(membership, Perm.ORGANIZATION_MANAGE)
    organization = Organization.objects.select_for_update().get(pk=membership.organization_id)
    if organization.logo:
        before = organization.logo.name
        _replace_logo(organization, data=None)
        audit.record(
            action=AuditEvent.Action.UPDATE,
            target=organization,
            membership=membership,
            changes={"logo": [before, ""]},
        )
    return organization


# ---------- Exportación completa de datos (X-03) ----------


@transaction.atomic
def request_data_export(*, membership) -> DataExport:
    """Pide el ZIP con todos los datos. Solo el administrador; una exportación en curso a la vez.

    Con `DATA_EXPORT_USE_QUEUE` lo arma el worker; si no, se arma al confirmar la
    transacción, en la misma petición (demo y desarrollo).
    """
    require_perm(membership, Perm.ORGANIZATION_MANAGE)
    organization = Organization.objects.select_for_update().get(pk=membership.organization_id)
    if DataExport.objects.filter(status=DataExport.Status.PENDING).exists():
        raise ValidationError(_("Ya hay una exportación en preparación. Te avisaremos cuando esté lista."))
    export = DataExport.objects.create(requested_by=membership.user)
    audit.record(action=AuditEvent.Action.CREATE, target=export, membership=membership)
    organization_id, export_id = str(organization.pk), str(export.pk)
    if settings.DATA_EXPORT_USE_QUEUE:
        from . import tasks

        transaction.on_commit(
            lambda: tasks.build_data_export.defer(organization_id=organization_id, export_id=export_id)
        )
    else:

        def run():
            with tenant_context(organization):
                build_data_export(export_id=export_id)

        transaction.on_commit(run)
    return export


def build_data_export(*, export_id) -> DataExport | None:
    """Arma y guarda el ZIP de una exportación pendiente y avisa a quien la pidió.

    Corre dentro del `tenant_context` de la organización (tarea o petición).
    """
    export = DataExport.objects.filter(pk=export_id, status=DataExport.Status.PENDING).first()
    if export is None:
        return None
    try:
        archive, _readme = exports.build_archive()
        with archive:
            export.file.save("export.zip", File(archive), save=False)
    except Exception as error:
        logger.exception("No se pudo armar la exportación %s", export_id)
        export.status = DataExport.Status.FAILED
        export.error = str(error)[:300]
        export.finished_at = timezone.now()
        export.save(update_fields=["status", "error", "finished_at", "updated_at"])
        return export
    now = timezone.now()
    export.status = DataExport.Status.READY
    export.size_bytes = export.file.size
    export.finished_at = now
    export.expires_at = now + datetime.timedelta(days=settings.DATA_EXPORT_TTL_DAYS)
    export.save(update_fields=["file", "status", "size_bytes", "finished_at", "expires_at", "updated_at"])
    _notify_export_ready(export)
    return export


def _notify_export_ready(export) -> None:
    from apps.accounts.models import Membership
    from apps.notifications.models import Kind
    from apps.notifications.services import deliver

    recipients = Membership.objects.select_related("user").filter(
        organization_id=export.organization_id, user_id=export.requested_by_id, is_active=True
    )
    deliver(
        kind=Kind.DATA_EXPORT_READY,
        memberships=list(recipients),
        title=_("Tu exportación de datos está lista"),
        body=_("Descárgala desde Configuración antes del %(date)s.")
        % {"date": timezone.localtime(export.expires_at).strftime("%d-%m-%Y")},
        url=reverse("organization_settings") + "#export",
    )


def data_export_for_download(*, membership, export_id, request=None) -> DataExport:
    """Exportación lista y vigente de la organización; registra la descarga en la auditoría."""
    require_perm(membership, Perm.ORGANIZATION_MANAGE)
    export = DataExport.objects.filter(
        pk=export_id, status=DataExport.Status.READY, expires_at__gt=timezone.now()
    ).first()
    if export is None or not export.file:
        raise DataExport.DoesNotExist
    with transaction.atomic():
        audit.record(action=AuditEvent.Action.DOWNLOAD, target=export, membership=membership, request=request)
    return export


def expire_data_exports(*, now=None) -> int:
    """Borra los ZIP vencidos de la organización del contexto (la fila queda en `expired`)."""
    now = now or timezone.now()
    expired = list(DataExport.objects.filter(status=DataExport.Status.READY, expires_at__lte=now))
    for export in expired:
        if export.file:
            export.file.storage.delete(export.file.name)
        export.file = ""
        export.status = DataExport.Status.EXPIRED
        export.save(update_fields=["file", "status", "updated_at"])
    return len(expired)


# ---------- Borrado de la organización (X-04) ----------


@transaction.atomic
def request_organization_deletion(*, membership, password, confirm_name) -> Organization:
    """Programa el borrado con período de gracia. Exige la clave y escribir el nombre exacto."""
    require_perm(membership, Perm.ORGANIZATION_MANAGE)
    organization = Organization.objects.select_for_update().get(pk=membership.organization_id)
    if organization.status == Organization.Status.PENDING_DELETION:
        raise ValidationError(_("La eliminación ya está solicitada."))
    if not membership.user.check_password(password or ""):
        raise ValidationError(_("La contraseña no es correcta."))
    if (confirm_name or "").strip().casefold() != organization.name.strip().casefold():
        raise ValidationError(_("Escribe el nombre de la organización tal como aparece."))
    now = timezone.now()
    before = audit.snapshot(organization, DELETION_FIELDS)
    organization.status = Organization.Status.PENDING_DELETION
    organization.deletion_requested_at = now
    organization.deletion_scheduled_for = now + datetime.timedelta(
        days=settings.ORGANIZATION_DELETION_GRACE_DAYS
    )
    organization.deletion_requested_by = membership.user
    organization.save(update_fields=[*DELETION_FIELDS, "updated_at"])
    audit.record(
        action=AuditEvent.Action.DELETION_REQUEST,
        target=organization,
        membership=membership,
        changes=audit.diff(before, audit.snapshot(organization, DELETION_FIELDS)),
    )
    return organization


@transaction.atomic
def cancel_organization_deletion(*, membership) -> Organization:
    require_perm(membership, Perm.ORGANIZATION_MANAGE)
    organization = Organization.objects.select_for_update().get(pk=membership.organization_id)
    if organization.status != Organization.Status.PENDING_DELETION:
        raise ValidationError(_("No hay una eliminación pendiente."))
    before = audit.snapshot(organization, DELETION_FIELDS)
    in_trial = organization.trial_ends_at and organization.trial_ends_at > timezone.now()
    organization.status = Organization.Status.TRIAL if in_trial else Organization.Status.ACTIVE
    organization.deletion_requested_at = None
    organization.deletion_scheduled_for = None
    organization.deletion_requested_by = None
    organization.save(update_fields=[*DELETION_FIELDS, "updated_at"])
    audit.record(
        action=AuditEvent.Action.DELETION_CANCEL,
        target=organization,
        membership=membership,
        changes=audit.diff(before, audit.snapshot(organization, DELETION_FIELDS)),
    )
    return organization
