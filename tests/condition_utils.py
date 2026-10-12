"""Ayudas de los tests de mantenimiento por condición (CBM)."""

import datetime

from apps.assets.models import Meter, MeterKind
from apps.assets.services import record_meter_reading

BASE = datetime.datetime(2026, 10, 1, 8, 0, tzinfo=datetime.UTC)


def variable(asset, kind=MeterKind.TEMPERATURE, name="Rodamiento"):
    return Meter.objects.create(asset=asset, kind=kind, name=name)


def read(meter, value, hours=0, **kwargs):
    """Lectura `hours` horas después de BASE (valores y fechas conocidos)."""
    at = BASE + datetime.timedelta(hours=hours)
    reading, _warnings = record_meter_reading(meter=meter, value=value, read_at=at, **kwargs)
    return reading
