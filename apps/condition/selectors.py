"""Lecturas de mantenimiento por condición (CBM-05, CBM-07): permisos, alcance por sitio y sin N+1."""

from django.db.models import Case, IntegerField, Q, Value, When

from apps.accounts.permissions import Perm, require_perm
from apps.accounts.scoping import allowed_location_ids
from apps.assets.models import Asset, Meter
from apps.organizations.selectors import descendant_ids

from .models import ConditionAlert, ConditionRule, ConditionState

Status = ConditionAlert.Status

# Abiertas primero, luego reconocidas, normalizadas y cerradas (CBM-05).
STATUS_ORDER = Case(
    When(status=Status.OPEN, then=Value(0)),
    When(status=Status.ACKNOWLEDGED, then=Value(1)),
    When(status=Status.NORMALIZED, then=Value(2)),
    default=Value(3),
    output_field=IntegerField(),
)
ALERT_RELATED = (
    "rule__asset_class",
    "rule__meter__asset",
    "meter__asset",
    "asset__location",
    "work_order",
    "acknowledged_by",
)


# --- Alertas ---------------------------------------------------------------------


def visible_alerts(*, membership):
    """Alertas de los activos que la membresía ve (ORG-06)."""
    require_perm(membership, Perm.CONDITION_VIEW)
    qs = ConditionAlert.objects.all()
    allowed = allowed_location_ids(membership)
    if allowed is not None:
        qs = qs.filter(asset__location_id__in=allowed)
    return qs


def alert_list(*, membership, status: str = "", location=None, asset=None):
    """Alertas abiertas primero y luego las más recientes; filtros por estado, sitio y activo.

    `status` = "active" filtra abiertas y reconocidas; `location` incluye sus sububicaciones.
    """
    qs = visible_alerts(membership=membership)
    if status == "active":
        qs = qs.filter(status__in=ConditionAlert.ACTIVE_STATUSES)
    elif status in Status.values:
        qs = qs.filter(status=status)
    if location is not None:
        qs = qs.filter(asset__location_id__in={location.pk, *descendant_ids(location)})
    if asset is not None:
        qs = qs.filter(asset=asset)
    return (
        qs.select_related(*ALERT_RELATED)
        .annotate(status_order=STATUS_ORDER)
        .order_by("status_order", "-created_at")
    )


def alert_for(*, membership, pk):
    """Una alerta visible para la membresía, o None."""
    return visible_alerts(membership=membership).select_related(*ALERT_RELATED).filter(pk=pk).first()


def active_alert_count(*, membership) -> int:
    """Alertas abiertas o reconocidas que la membresía ve (contador del inicio)."""
    return visible_alerts(membership=membership).filter(status__in=ConditionAlert.ACTIVE_STATUSES).count()


def asset_alerts(*, membership, asset, limit: int = 20) -> list:
    """Últimas alertas de un activo (ficha del activo, CBM-07)."""
    return list(alert_list(membership=membership, asset=asset)[:limit])


# --- Reglas ----------------------------------------------------------------------


def _visible_rule_scope(membership) -> Q:
    """Reglas por clase: todas. Reglas por medidor: solo si el activo está en el alcance."""
    allowed = allowed_location_ids(membership)
    if allowed is None:
        return Q()
    return Q(meter__isnull=True) | Q(meter__asset__location_id__in=allowed)


def visible_rules(*, membership, include_inactive: bool = False):
    require_perm(membership, Perm.CONDITION_VIEW)
    qs = ConditionRule.objects.filter(_visible_rule_scope(membership))
    if not include_inactive:
        qs = qs.filter(is_active=True)
    return qs


def rule_list(*, membership, include_inactive: bool = False, asset_class=None, meter=None):
    """Reglas (activas, o todas) con su alcance; filtra por clase o por medidor."""
    qs = visible_rules(membership=membership, include_inactive=include_inactive)
    if asset_class is not None:
        qs = qs.filter(asset_class=asset_class)
    if meter is not None:
        qs = qs.filter(pk__in=rules_for_meter(meter, include_inactive=True).values("pk"))
    return qs.select_related("meter__asset", "asset_class").order_by("-is_active", "created_at")


def rule_for(*, membership, pk):
    return (
        visible_rules(membership=membership, include_inactive=True)
        .select_related("meter__asset", "asset_class")
        .filter(pk=pk)
        .first()
    )


def rules_for_meter(meter: Meter, *, include_inactive: bool = False):
    """Reglas que aplican a una variable: las del medidor y las de la clase de su activo con su
    (tipo, nombre). Una sola consulta (la clase sale por subconsulta)."""
    asset_class = Asset.objects.filter(pk=meter.asset_id).values("asset_class_id")
    qs = ConditionRule.objects.filter(
        Q(meter_id=meter.pk)
        | Q(meter__isnull=True, asset_class_id__in=asset_class, meter_kind=meter.kind, meter_name=meter.name)
    )
    if not include_inactive:
        qs = qs.filter(is_active=True)
    return qs.order_by("created_at")


# --- Estado por variable (ficha del activo, CBM-07) --------------------------------


def meter_states(*, meter_ids) -> dict:
    """{meter_id: [ConditionState de reglas activas]} para pintar el estado de cada variable."""
    states: dict = {}
    for state in (
        ConditionState.objects.filter(meter_id__in=list(meter_ids), rule__is_active=True)
        .select_related("rule")
        .order_by("created_at")
    ):
        states.setdefault(state.meter_id, []).append(state)
    return states
