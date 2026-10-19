"""Matriz rol → permisos (ADR-013) y roles personalizados (ORG-12, ADR-031).

Se verifica en servicios y vistas con `has_perm` / `require_perm`.
"""

from enum import StrEnum

from django.utils.translation import gettext_lazy as _

from apps.core.tenancy import get_current_organization

from .models import CustomRole, Membership, load_custom_role

Role = Membership.Role


class Perm(StrEnum):
    ASSET_MANAGE = "asset.manage"
    PLAN_MANAGE = "plan.manage"
    WORK_ORDER_MANAGE = "work_order.manage"  # crear, asignar, cancelar, aprobar
    WORK_ORDER_EXECUTE = "work_order.execute"  # iniciar, checklist, completar
    REQUEST_CREATE = "request.create"  # avisar falla / pedir trabajo
    METER_READING_CREATE = "meter_reading.create"  # horómetro / odómetro (choferes, operadores)
    TEAM_MANAGE = "team.manage"  # invitar, cambiar rol y alcance, desactivar (ORG-05/06)
    AUDIT_VIEW = "audit.view"  # consultar la auditoría (X-01)
    ORGANIZATION_MANAGE = "organization.manage"  # configuración de la organización (WO-02 CA2, REQ-03 CA2)
    API_TOKEN_MANAGE = "api_token.manage"  # crear y revocar tokens de la API (X-05)
    # Inventario (INV-16). Consumir en OT es WORK_ORDER_EXECUTE; reservar, WORK_ORDER_MANAGE.
    INVENTORY_VIEW = "inventory.view"  # catálogo, stock y movimientos (sin costos)
    INVENTORY_COST_VIEW = "inventory.cost_view"  # costo promedio, valor y costos de movimientos
    INVENTORY_MOVE = "inventory.move"  # entradas y transferencias
    INVENTORY_MANAGE = "inventory.manage"  # catálogo, bodegas, conteos, mínimos, importación
    # Mantenimiento por condición (CBM-08). Registrar lecturas sigue siendo METER_READING_CREATE.
    CONDITION_VIEW = "condition.view"  # alertas y reglas de condición
    CONDITION_MANAGE = "condition.manage"  # crear y editar reglas, reconocer alertas
    # Asistente de diagnóstico (AI-01..05). Cada uno ve sus conversaciones; siempre con alcance por sitio.
    ASSISTANT_USE = "assistant.use"  # pedir ayuda sobre un activo visible
    ASSISTANT_VIEW_ALL = "assistant.view_all"  # ver las conversaciones de toda la organización (AI-05 CA3)


# Permisos de administración: solo el rol base `admin` los tiene. Un rol personalizado nunca
# los incluye (ORG-12 CA3): así no hay escalamiento y "siempre queda un administrador" no cambia.
ADMIN_ONLY_PERMS: frozenset[Perm] = frozenset(
    {
        Perm.TEAM_MANAGE,
        Perm.AUDIT_VIEW,
        Perm.ORGANIZATION_MANAGE,
        Perm.API_TOKEN_MANAGE,
        Perm.ASSISTANT_VIEW_ALL,
    }
)
CUSTOMIZABLE_PERMS: frozenset[Perm] = frozenset(Perm) - ADMIN_ONLY_PERMS

ROLE_PERMISSIONS: dict[str, frozenset[Perm]] = {
    Role.ADMIN: frozenset(Perm),
    Role.PLANNER: CUSTOMIZABLE_PERMS,
    Role.SUPERVISOR: frozenset(
        {
            Perm.WORK_ORDER_MANAGE,
            Perm.WORK_ORDER_EXECUTE,
            Perm.REQUEST_CREATE,
            Perm.METER_READING_CREATE,
            Perm.INVENTORY_VIEW,
            Perm.INVENTORY_COST_VIEW,
            Perm.INVENTORY_MOVE,
            Perm.CONDITION_VIEW,
            Perm.ASSISTANT_USE,
        }
    ),
    Role.TECHNICIAN: frozenset(
        {
            Perm.WORK_ORDER_EXECUTE,
            Perm.REQUEST_CREATE,
            Perm.METER_READING_CREATE,
            Perm.INVENTORY_VIEW,
            Perm.CONDITION_VIEW,
            Perm.ASSISTANT_USE,
        }
    ),
    Role.VIEWER: frozenset(
        {
            Perm.REQUEST_CREATE,
            Perm.METER_READING_CREATE,
            Perm.INVENTORY_VIEW,
            Perm.INVENTORY_COST_VIEW,
            Perm.CONDITION_VIEW,
        }
    ),
    Role.EXTERNAL: frozenset({Perm.REQUEST_CREATE}),
}


# Pantalla de roles personalizados: permisos elegibles agrupados por área, con su etiqueta.
PERM_LABELS: dict[Perm, str] = {
    Perm.ASSET_MANAGE: _("Crear y editar activos, ubicaciones, clases y medidores"),
    Perm.PLAN_MANAGE: _("Crear y editar planes preventivos"),
    Perm.WORK_ORDER_MANAGE: _("Crear, asignar, programar, cancelar y aprobar OT; revisar solicitudes"),
    Perm.WORK_ORDER_EXECUTE: _("Ejecutar OT: iniciar, checklist, horas, repuestos y completar"),
    Perm.REQUEST_CREATE: _("Avisar fallas y pedir trabajos"),
    Perm.METER_READING_CREATE: _("Registrar lecturas de medidores"),
    Perm.INVENTORY_VIEW: _("Ver catálogo, stock y movimientos"),
    Perm.INVENTORY_COST_VIEW: _("Ver costos y valor del inventario"),
    Perm.INVENTORY_MOVE: _("Registrar entradas y transferencias"),
    Perm.INVENTORY_MANAGE: _("Administrar catálogo, bodegas, conteos y mínimos"),
    Perm.CONDITION_VIEW: _("Ver alertas y reglas de condición"),
    Perm.CONDITION_MANAGE: _("Crear reglas y reconocer alertas de condición"),
    Perm.ASSISTANT_USE: _("Usar el asistente de diagnóstico"),
}
PERM_GROUPS: list[tuple[str, str, list[Perm]]] = [
    (
        "work",
        _("Órdenes de trabajo y solicitudes"),
        [
            Perm.WORK_ORDER_MANAGE,
            Perm.WORK_ORDER_EXECUTE,
            Perm.REQUEST_CREATE,
            Perm.METER_READING_CREATE,
        ],
    ),
    ("assets", _("Activos y preventivos"), [Perm.ASSET_MANAGE, Perm.PLAN_MANAGE]),
    (
        "inventory",
        _("Inventario"),
        [
            Perm.INVENTORY_VIEW,
            Perm.INVENTORY_COST_VIEW,
            Perm.INVENTORY_MOVE,
            Perm.INVENTORY_MANAGE,
        ],
    ),
    ("condition", _("Mantenimiento por condición"), [Perm.CONDITION_VIEW, Perm.CONDITION_MANAGE]),
    ("assistant", _("Asistente de diagnóstico"), [Perm.ASSISTANT_USE]),
]

CUSTOM_ROLES_MODULE = "custom_roles"


class PermissionDeniedError(PermissionError):
    pass


def _organization_of(membership):
    # El contexto ya trae la organización (con su plan): evita una consulta por membresía.
    current = get_current_organization()
    if current is not None and current.pk == membership.organization_id:
        return current
    return membership.organization


def active_custom_role(membership) -> CustomRole | None:
    """Rol personalizado que rige la membresía, o None si rige su rol base.

    Rige si está activo y el plan incluye el módulo `custom_roles`; si el plan lo pierde, la
    membresía cae a los permisos de su rol base sin borrar nada (ORG-12 CA6).
    """
    if membership is None or getattr(membership, "custom_role_id", None) is None:
        return None
    from apps.organizations.modules import has_module  # modules importa este módulo

    if not has_module(_organization_of(membership), CUSTOM_ROLES_MODULE):
        return None
    role = load_custom_role(membership)
    return role if role is not None and role.is_active else None


def effective_permissions(membership) -> frozenset[Perm]:
    """Permisos vigentes: los del rol personalizado activo o los del rol base."""
    if membership is None:
        return frozenset()
    custom = active_custom_role(membership)
    if custom is not None:
        return custom.permission_set
    return ROLE_PERMISSIONS.get(membership.role, frozenset())


def has_perm(membership, perm: Perm) -> bool:
    return perm in effective_permissions(membership)


def require_perm(membership, perm: Perm) -> None:
    if not has_perm(membership, perm):
        raise PermissionDeniedError(perm)
