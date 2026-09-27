"""Purga de organizaciones con el borrado vencido (X-04). Código de plataforma.

Tras el período de gracia se borran, para esa organización y solo esa:
- las filas de todas las tablas `TenantModel` (descubiertas, no listadas);
- sus membresías e invitaciones, y los usuarios de servicio de sus tokens de API;
- todos sus archivos bajo `org/<organization_id>/` (adjuntos, logo, exportaciones).

La fila de `Organization` queda como lápida anonimizada (id, fechas y
`purge_summary` con los conteos): es el registro de plataforma que sobrevive a
la purga (el registro de auditoría de la organización se borra con ella). Las
cuentas de usuario no se borran: quien solo pertenecía a esa organización
conserva su cuenta, sin datos ni membresías.

Corre con `platform_context()` (RLS desactivado, ADR-026): es la única forma de
borrar todas las filas de una organización fuera de una petición. Cada borrado
filtra por `organization_id` explícitamente.
"""

import logging

from django.apps import apps
from django.core.files.storage import default_storage
from django.db import transaction
from django.utils import timezone

from apps.core.models import TenantModel
from apps.core.tenancy import platform_context, tenant_context

from .models import Organization

logger = logging.getLogger(__name__)

API_SERVICE_USER_DOMAIN = "@api.galbust.invalid"


def tenant_models() -> list:
    return [
        model
        for model in apps.get_models()
        if issubclass(model, TenantModel) and model._meta.managed and not model._meta.proxy
    ]


def due_organizations(*, now=None):
    now = now or timezone.now()
    return Organization.objects.filter(
        status=Organization.Status.PENDING_DELETION, deletion_scheduled_for__lte=now
    ).order_by("deletion_scheduled_for")


def purge_due_organizations(*, now=None) -> list[str]:
    purged = []
    for organization in due_organizations(now=now):
        purge_organization(organization)
        purged.append(str(organization.pk))
    return purged


def expire_all_data_exports(*, now=None) -> int:
    """Borra los ZIP de exportación vencidos (X-03), organización por organización."""
    from .models import DataExport
    from .services import expire_data_exports

    now = now or timezone.now()
    # Plataforma: solo se buscan las organizaciones con algo vencido; el borrado va con su contexto.
    org_ids = set(
        DataExport.all_tenants.filter(status=DataExport.Status.READY, expires_at__lte=now).values_list(
            "organization_id", flat=True
        )
    )
    expired = 0
    for organization in Organization.objects.filter(pk__in=org_ids):
        with tenant_context(organization):
            expired += expire_data_exports(now=now)
    return expired


def run_daily(*, now=None) -> int:
    """Tarea diaria (y `manage.py purge_organizations`): vence exportaciones y purga lo vencido."""
    expire_all_data_exports(now=now)
    return len(purge_due_organizations(now=now))


def _delete_prefix(storage, prefix: str) -> int:
    """Borra todo lo que hay bajo `prefix` (recursivo); devuelve los archivos borrados."""
    try:
        directories, files = storage.listdir(prefix)
    except (FileNotFoundError, NotADirectoryError):
        return 0
    deleted = 0
    for name in files:
        storage.delete(f"{prefix}{name}")
        deleted += 1
    for directory in directories:
        deleted += _delete_prefix(storage, f"{prefix}{directory}/")
    return deleted


def purge_organization(organization) -> dict:
    """Borra los datos y archivos de una organización y deja su lápida. Idempotente."""
    from apps.accounts.models import Invitation, Membership, User
    from apps.api.models import ApiToken

    org_id = organization.pk
    counts = {}
    # Plataforma (X-04): la purga debe ver y borrar todas las filas de esta organización.
    with platform_context(), transaction.atomic():
        organization = Organization.objects.select_for_update().get(pk=org_id)
        if organization.status == Organization.Status.DELETED:
            return organization.purge_summary
        service_user_ids = list(
            ApiToken.all_tenants.filter(organization_id=org_id).values_list("service_user_id", flat=True)
        )
        # Las FK de Django son DEFERRABLE INITIALLY DEFERRED: se verifican al confirmar,
        # cuando ya no queda ninguna fila de la organización, así que el orden no importa.
        for model in tenant_models():
            queryset = model._base_manager.filter(organization_id=org_id)
            deleted = queryset._raw_delete(queryset.db)
            if deleted:
                counts[model._meta.label_lower] = deleted
        counts["accounts.membership"] = Membership.objects.filter(organization_id=org_id).delete()[0]
        counts["accounts.invitation"] = Invitation.objects.filter(organization_id=org_id).delete()[0]
        User.objects.filter(last_organization_id=org_id).update(last_organization=None)
        # Usuarios técnicos de los tokens de API: sin la organización no representan a nadie.
        User.objects.filter(pk__in=service_user_ids, email__endswith=API_SERVICE_USER_DOMAIN).delete()

        summary = {
            "rows": sum(counts.values()),
            "tables": counts,
            "requested_at": organization.deletion_requested_at.isoformat()
            if organization.deletion_requested_at
            else None,
            "requested_by": str(organization.deletion_requested_by_id or ""),
        }
        logo_name = organization.logo.name
        organization.name = "—"
        organization.slug = f"deleted-{org_id.hex}"
        organization.status = Organization.Status.DELETED
        organization.is_active = False
        organization.logo = ""
        organization.logo_size_bytes = 0
        organization.primary_color = ""
        organization.work_order_status_labels = {}
        organization.deleted_at = timezone.now()
        organization.purge_summary = summary
        organization.save()

    # Archivos: después de confirmar (si la base falla, no se pierden archivos).
    files = _delete_prefix(default_storage, f"org/{org_id}/")
    if logo_name and default_storage.exists(logo_name):
        default_storage.delete(logo_name)
    summary["files"] = files
    Organization.objects.filter(pk=org_id).update(purge_summary=summary)
    logger.info(
        "Organización %s purgada: %s filas, %s archivos",
        org_id,
        summary["rows"],
        files,
        extra={"organization_id": str(org_id), "purge_summary": summary},
    )
    return summary
