"""Matriz rol → permisos (ADR-013). Se verifica en servicios y vistas."""

from enum import StrEnum

from .models import Membership

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


ROLE_PERMISSIONS: dict[str, frozenset[Perm]] = {
    Role.ADMIN: frozenset(Perm),
    Role.PLANNER: frozenset(Perm)
    - {Perm.TEAM_MANAGE, Perm.AUDIT_VIEW, Perm.ORGANIZATION_MANAGE, Perm.API_TOKEN_MANAGE},
    Role.SUPERVISOR: frozenset(
        {
            Perm.WORK_ORDER_MANAGE,
            Perm.WORK_ORDER_EXECUTE,
            Perm.REQUEST_CREATE,
            Perm.METER_READING_CREATE,
            Perm.INVENTORY_VIEW,
            Perm.INVENTORY_COST_VIEW,
            Perm.INVENTORY_MOVE,
        }
    ),
    Role.TECHNICIAN: frozenset(
        {Perm.WORK_ORDER_EXECUTE, Perm.REQUEST_CREATE, Perm.METER_READING_CREATE, Perm.INVENTORY_VIEW}
    ),
    Role.VIEWER: frozenset(
        {Perm.REQUEST_CREATE, Perm.METER_READING_CREATE, Perm.INVENTORY_VIEW, Perm.INVENTORY_COST_VIEW}
    ),
    Role.EXTERNAL: frozenset({Perm.REQUEST_CREATE}),
}


class PermissionDeniedError(PermissionError):
    pass


def has_perm(membership, perm: Perm) -> bool:
    return membership is not None and perm in ROLE_PERMISSIONS.get(membership.role, ())


def require_perm(membership, perm: Perm) -> None:
    if not has_perm(membership, perm):
        raise PermissionDeniedError(perm)
