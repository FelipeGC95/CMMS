"""Lecturas de la API de terreno (`/field/api/`, X-06, ADR-011): lo que el teléfono descarga.

Arman `bootstrap`, `snapshot` y `reference` con los selectores de `apps.work`; las vistas
solo validan la entrada y serializan. Reciben la membresía real de la sesión (alcance por
sitio y rol), así el teléfono ve exactamente lo mismo que la web.
"""

from django.utils import timezone
from django.utils.translation import gettext as _
from rest_framework.exceptions import ValidationError

from apps.accounts.permissions import Perm, has_perm
from apps.billing.access import is_read_only
from apps.inventory.selectors import field_parts
from apps.ui.templatetags.ui import work_order_status_label
from apps.work.models import WorkOrder
from apps.work.selectors import (
    FIELD_DEFAULT_DAYS,
    FIELD_MAX_DAYS,
    field_reference,
    field_work_orders,
)

from .framework import MIN_CLIENT_VERSION

S = WorkOrder.Status

# Transiciones que el teléfono hace sin conexión: solo las de ejecución (ADR-011).
EXECUTION_TRANSITIONS = {
    "start": (S.IN_PROGRESS, (S.REQUESTED, S.PLANNED, S.SCHEDULED)),
    "pause": (S.ON_HOLD, (S.IN_PROGRESS,)),
    "resume": (S.IN_PROGRESS, (S.ON_HOLD,)),
    "complete": (S.COMPLETED, (S.IN_PROGRESS,)),
}


def _execution_transitions() -> dict:
    return {
        name: {"from": list(from_statuses), "to": to_status}
        for name, (to_status, from_statuses) in EXECUTION_TRANSITIONS.items()
    }


def bootstrap(*, membership) -> dict:
    """Quién es la persona, su organización, qué puede hacer y la versión mínima del cliente.

    El teléfono compara `user.id` y `organization.id` con los de su base local y de cada
    cambio en cola antes de sincronizar (ADR-011).
    """
    organization = membership.organization
    return {
        "user": membership.user,
        "organization": organization,
        "role": membership.role,
        "permissions": {
            "execute": has_perm(membership, Perm.WORK_ORDER_EXECUTE),
            "request_create": has_perm(membership, Perm.REQUEST_CREATE),
            "reading_create": has_perm(membership, Perm.METER_READING_CREATE),
        },
        "horizon_days": FIELD_DEFAULT_DAYS,
        "status_labels": {status: str(work_order_status_label(status)) for status in S.values},
        "execution_transitions": _execution_transitions(),
        "server_time": timezone.now(),
        "min_client_version": MIN_CLIENT_VERSION,
        # Solo lectura por facturación (ORG-13): el teléfono muestra el aviso; la cola se envía al pagar.
        "read_only": is_read_only(organization),
    }


def parse_days(raw) -> int:
    """`?days=N` del snapshot: por defecto 7, entre 0 y 14."""
    if raw in (None, ""):
        return FIELD_DEFAULT_DAYS
    try:
        days = int(raw)
    except (TypeError, ValueError):
        days = -1
    if not 0 <= days <= FIELD_MAX_DAYS:
        raise ValidationError({"days": [_("Indica un número de días entre 0 y 14.")]})
    return days


def snapshot(*, membership, days: int) -> dict:
    """OT visibles para trabajar sin conexión (ver `field_work_orders`), con tope y `truncated`."""
    result = field_work_orders(membership=membership, days=days)
    return {
        "generated_at": timezone.now(),
        "days": days,
        "truncated": result.truncated,
        "work_orders": result.work_orders,
    }


def reference(*, membership) -> dict:
    """Catálogos: códigos de falla activos por tipo, activos visibles (tope 2000), medidores y
    repuestos activos (tope 2000, solo con el módulo `inventory` y `inventory.view`)."""
    result = field_reference(membership=membership)
    parts, parts_truncated = field_parts(membership=membership)
    return {
        "failure_codes": result.failure_codes,
        "assets": result.assets,
        "truncated": result.assets_truncated,
        "meters": result.meters,
        "parts": parts,
        "parts_truncated": parts_truncated,
    }
