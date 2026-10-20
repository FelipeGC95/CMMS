from apps.accounts.permissions import Perm, has_perm
from apps.organizations.modules import has_module

from .models import Kind, Notification, NotificationPreference

# Permiso del rol que hace posible recibir cada tipo de aviso (X-02).
KIND_PERMS = {
    Kind.WO_ASSIGNED: Perm.WORK_ORDER_EXECUTE,  # solo se asigna a quien ejecuta OT
    Kind.WO_SCHEDULED: Perm.WORK_ORDER_EXECUTE,
    Kind.WO_DUE_SOON: Perm.WORK_ORDER_EXECUTE,  # responsables y apoyo, o quienes gestionan
    Kind.WO_OVERDUE: Perm.WORK_ORDER_EXECUTE,
    Kind.REQUEST_NEW: Perm.WORK_ORDER_MANAGE,
    Kind.REQUEST_RESOLVED: Perm.REQUEST_CREATE,
    Kind.DATA_EXPORT_READY: Perm.ORGANIZATION_MANAGE,
    Kind.STOCK_LOW: Perm.INVENTORY_MOVE,  # quienes reponen (INV-12)
    Kind.CONDITION_ALERT: Perm.WORK_ORDER_MANAGE,  # quienes deciden la OT predictiva (CBM-05)
    Kind.WEBHOOK_DISABLED: Perm.API_TOKEN_MANAGE,  # quienes administran integraciones (X-08)
    Kind.BILLING: Perm.ORGANIZATION_MANAGE,  # administradores: fin de prueba, impago (ORG-13)
}
# Módulo de plan que además exige el tipo (ORG-10): sin él, el aviso no existe para nadie.
KIND_MODULES = {Kind.STOCK_LOW: "inventory", Kind.CONDITION_ALERT: "condition", Kind.WEBHOOK_DISABLED: "api"}


def receivable_kinds(*, membership) -> list[str]:
    """Tipos que el rol de la persona en la organización activa puede recibir."""
    organization = getattr(membership, "organization", None)
    return [
        kind
        for kind in Kind.values
        if has_perm(membership, KIND_PERMS[kind])
        and (kind not in KIND_MODULES or has_module(organization, KIND_MODULES[kind]))
    ]


def notification_list(*, membership):
    """Notificaciones propias en la app, de la más reciente a la más antigua."""
    return Notification.objects.filter(recipient=membership.user, in_app=True).order_by("-created_at")


def unread_count(*, membership) -> int:
    """Contador de la campana: una sola consulta sobre el índice de no leídas."""
    return Notification.objects.filter(recipient=membership.user, in_app=True, read_at__isnull=True).count()


def preference_rows(*, membership) -> list[dict]:
    """Una fila por tipo que el rol puede recibir, con sus dos canales (los no guardados, activos).

    Las preferencias guardadas de tipos ocultos se conservan: si el rol cambia, vuelven.
    """
    visible = set(receivable_kinds(membership=membership))
    saved = {
        kind: (in_app, email)
        for kind, in_app, email in NotificationPreference.objects.filter(user=membership.user).values_list(
            "kind", "in_app", "email"
        )
    }
    return [
        {"kind": kind, "label": label, "in_app": in_app, "email": email}
        for kind, label in Kind.choices
        if kind in visible
        for in_app, email in [saved.get(kind, (True, True))]
    ]
