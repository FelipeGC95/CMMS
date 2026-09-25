import datetime
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from apps.accounts.permissions import Perm, require_perm

from .models import Asset, AssetStatusPeriod, Meter, MeterKind, MeterReading

# Más de 24 h de uso por día en un horómetro indica un error de digitación.
MAX_HOURS_PER_DAY = Decimal("24")
USAGE_WINDOW_DAYS = 30


class MeterReadingError(ValidationError):
    pass


@transaction.atomic
def change_asset_status(*, asset: Asset, status: str, by=None, reason: str = "", at=None) -> Asset:
    """Cierra el período de estado vigente y abre uno nuevo."""
    if asset.status == status:
        return asset
    at = at or timezone.now()
    AssetStatusPeriod.objects.filter(asset=asset, ended_at__isnull=True).update(ended_at=at)
    AssetStatusPeriod.objects.create(
        asset=asset, status=status, started_at=at, reason=reason[:200], changed_by=by
    )
    asset.status = status
    asset.save(update_fields=["status", "updated_at"])
    return asset


@transaction.atomic
def record_meter_reading(
    *,
    meter: Meter,
    value,
    membership=None,
    read_at: datetime.datetime | None = None,
    is_reset: bool = False,
    source: str = MeterReading.Source.MANUAL,
    work_order=None,
    client_uuid=None,
    generate_work_orders: bool = True,
) -> tuple[MeterReading, list[str]]:
    """Registra una lectura (AST-07) y dispara los preventivos por uso que venzan.

    Devuelve la lectura y una lista de advertencias (no bloquean el registro).
    `membership` es None solo en procesos del sistema (importación, API de
    plataforma, datos demo).
    """
    if membership is not None:
        require_perm(membership, Perm.METER_READING_CREATE)
    if client_uuid:
        existing = MeterReading.objects.filter(client_uuid=client_uuid).first()
        if existing:
            return existing, []

    value = Decimal(str(value))
    read_at = read_at or timezone.now()
    if value < 0:
        raise MeterReadingError("La lectura no puede ser negativa.")

    previous_value, previous_at = meter.current_value, meter.current_read_at
    is_latest = previous_at is None or read_at >= previous_at
    warnings = []
    if meter.is_cumulative and previous_value is not None and is_latest and not is_reset:
        if value < previous_value:
            raise MeterReadingError(
                f"La lectura ({value.normalize():f}) es menor que la actual "
                f"({previous_value.normalize():f}). Si se cambió o reinició el medidor, márcalo."
            )
        elapsed_days = Decimal((read_at - previous_at).total_seconds()) / 86400
        if meter.kind == MeterKind.HOURS and elapsed_days > 0:
            if (value - previous_value) / elapsed_days > MAX_HOURS_PER_DAY:
                warnings.append("Uso mayor a 24 h por día desde la lectura anterior: revisa el valor.")

    reading = MeterReading.objects.create(
        meter=meter,
        value=value,
        read_at=read_at,
        source=source,
        is_reset=is_reset,
        work_order=work_order,
        created_by=membership.user if membership else None,
        client_uuid=client_uuid,
    )
    if not is_latest:
        return reading, warnings  # lectura histórica: no cambia el valor vigente

    meter.current_value = value
    meter.current_read_at = read_at
    meter.avg_daily_usage = _average_daily_usage(meter, until=read_at)
    meter.save(update_fields=["current_value", "current_read_at", "avg_daily_usage", "updated_at"])

    from apps.maintenance import services as maintenance

    if is_reset and previous_value is not None:
        maintenance.shift_meter_targets(meter=meter, delta=value - previous_value)
    if generate_work_orders:
        maintenance.generate_due_work_orders(meter=meter)
    return reading, warnings


def _average_daily_usage(meter: Meter, *, until) -> Decimal | None:
    """Uso por día entre la primera y la última lectura de la ventana, sin cruzar reinicios."""
    since = until - datetime.timedelta(days=USAGE_WINDOW_DAYS)
    readings = list(
        meter.readings.filter(read_at__gte=since, read_at__lte=until)
        .order_by("read_at")
        .values_list("read_at", "value", "is_reset")
    )
    last_reset = max((i for i, r in enumerate(readings) if r[2]), default=0)
    readings = readings[last_reset:]
    if len(readings) < 2:
        return meter.avg_daily_usage
    (first_at, first_value, _), (last_at, last_value, _) = readings[0], readings[-1]
    days = Decimal((last_at - first_at).total_seconds()) / 86400
    if days < 1:
        return meter.avg_daily_usage
    return ((last_value - first_value) / days).quantize(Decimal("0.001"))
