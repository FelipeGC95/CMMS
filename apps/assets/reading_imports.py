"""Importación masiva de lecturas de medidor por CSV o API (AST-09).

Mismo patrón que la importación de activos (AST-08): `build_reading_preview`
valida sin escribir y `apply_reading_import` guarda las filas válidas, una a
una con `record_meter_reading` (mismas reglas que una lectura manual: sin
negativos, acumulativas que no bajan salvo reinicio, preventivos por uso).

Idempotente por (medidor, fecha, valor): una lectura que ya existe se omite,
así reenviar el mismo archivo o lote no duplica nada. Cada lectura lleva además
un `client_uuid` derivado de esa clave (dos confirmaciones simultáneas tampoco
duplican).

Cuando el activo tiene varios medidores o variables (CBM-02 CA2), las columnas
opcionales `tipo` y `nombre` eligen cuál: la clave de una variable en el activo
es (tipo, nombre). Los CSV sin esas columnas se siguen leyendo igual.
"""

import csv
import datetime
import io
import re
import unicodedata
import uuid
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError
from django.db.models import Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.utils.translation import gettext

from .models import Meter, MeterKind, MeterReading
from .selectors import visible_assets
from .services import MeterReadingError, record_meter_reading

MAX_ROWS = 2000
MAX_FILE_BYTES = 2 * 1024 * 1024
# Tolerancia para relojes de equipos adelantados.
FUTURE_TOLERANCE = datetime.timedelta(minutes=5)

HEADER_ALIASES = {
    "asset_code": "asset_code",
    "asset": "asset_code",
    "activo": "asset_code",
    "codigo": "asset_code",
    "codigo_activo": "asset_code",
    "meter": "meter",
    "meter_id": "meter",
    "medidor": "meter",
    "id_medidor": "meter",
    "value": "value",
    "valor": "value",
    "lectura": "value",
    "read_at": "read_at",
    "fecha": "read_at",
    "fecha_lectura": "read_at",
    "date": "read_at",
    "is_reset": "is_reset",
    "reset": "is_reset",
    "reinicio": "is_reset",
    # Elegir la variable o el medidor del activo por (tipo, nombre) (CBM-02 CA2).
    "kind": "kind",
    "tipo": "kind",
    "meter_kind": "kind",
    "tipo_medidor": "kind",
    "tipo_variable": "kind",
    "name": "name",
    "nombre": "name",
    "meter_name": "name",
    "nombre_medidor": "name",
    "nombre_variable": "name",
    "variable": "name",
}
# Valores aceptados en la columna `tipo`: el código, la etiqueta y sinónimos en español.
KIND_ALIASES = {
    "hours": MeterKind.HOURS,
    "horas": MeterKind.HOURS,
    "horometro": MeterKind.HOURS,
    "km": MeterKind.KM,
    "kilometros": MeterKind.KM,
    "odometro": MeterKind.KM,
    "cycles": MeterKind.CYCLES,
    "ciclos": MeterKind.CYCLES,
    "other": MeterKind.OTHER,
    "otro": MeterKind.OTHER,
    "temperature": MeterKind.TEMPERATURE,
    "temperatura": MeterKind.TEMPERATURE,
    "vibration": MeterKind.VIBRATION,
    "vibracion": MeterKind.VIBRATION,
    "pressure": MeterKind.PRESSURE,
    "presion": MeterKind.PRESSURE,
    "current": MeterKind.CURRENT,
    "corriente": MeterKind.CURRENT,
    "variable": MeterKind.VARIABLE,
    "otra": MeterKind.VARIABLE,
    "otra_variable": MeterKind.VARIABLE,
}
# Espacio de nombres de los `client_uuid` derivados de (medidor, fecha, valor).
READING_NAMESPACE = uuid.UUID("6f1c2d8e-5a47-4c3b-9e0d-2b7f4a1c9e55")
TRUE_VALUES = {"1", "si", "s", "yes", "y", "true", "x", "verdadero"}
DATE_FORMATS = ("%d-%m-%Y %H:%M", "%d/%m/%Y %H:%M", "%d-%m-%Y", "%d/%m/%Y", "%Y-%m-%d")


class ReadingImportFileError(ValidationError):
    """El archivo no se puede leer (las filas se informan una a una)."""


def _normalize(text) -> str:
    text = unicodedata.normalize("NFKD", str(text)).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def parse_csv(uploaded_file) -> list[dict]:
    """Lee el CSV (coma o punto y coma, UTF-8 con o sin BOM). Devuelve filas como texto."""
    if uploaded_file.size > MAX_FILE_BYTES:
        raise ReadingImportFileError(gettext("El archivo supera 2 MB."))
    raw = uploaded_file.read()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")
    first_line = text.split("\n", 1)[0]
    delimiter = ";" if first_line.count(";") > first_line.count(",") else ","
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    header = next(reader, None) or []
    keys = [HEADER_ALIASES.get(_normalize(h)) for h in header]
    missing = []
    if "asset_code" not in keys and "meter" not in keys:
        missing.append(gettext("código de activo o id de medidor"))
    missing += [label for key, label in (("value", "value"), ("read_at", "read_at")) if key not in keys]
    if missing:
        raise ReadingImportFileError(
            gettext("Faltan columnas obligatorias: %(columns)s.") % {"columns": ", ".join(missing)}
        )
    rows = []
    for number, values in enumerate(reader, start=2):
        row = {key: value.strip() for key, value in zip(keys, values, strict=False) if key}
        if not any(row.values()):
            continue
        if len(rows) >= MAX_ROWS:
            raise ReadingImportFileError(
                gettext("El archivo tiene más de %(max)s filas; divídelo en partes.") % {"max": MAX_ROWS}
            )
        row["_row"] = number
        rows.append(row)
    if not rows:
        raise ReadingImportFileError(gettext("El archivo no tiene filas con datos."))
    return rows


def parse_value(raw) -> Decimal:
    text = str(raw).strip().replace(" ", "")
    if "," in text:
        text = text.replace(".", "").replace(",", ".")  # 1.234,5 → 1234.5
    try:
        value = Decimal(text)
    except InvalidOperation as exc:
        raise ValueError from exc
    if not value.is_finite():
        raise ValueError
    return value


def parse_read_at(raw) -> datetime.datetime:
    """ISO 8601 o dd-mm-aaaa [hh:mm]; sin zona se entiende en la zona de la organización."""
    if isinstance(raw, datetime.datetime):
        parsed = raw
    else:
        text = str(raw).strip()
        parsed = parse_datetime(text.replace(" ", "T", 1)) if len(text) > 10 else None
        for fmt in DATE_FORMATS:
            if parsed is not None:
                break
            try:
                parsed = datetime.datetime.strptime(text, fmt)
            except ValueError:
                continue
        if parsed is None:
            raise ValueError
    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed)
    return parsed


def parse_kind(raw) -> str | None:
    """Tipo de medidor desde el CSV: código (`temperature`), etiqueta (`Temperatura (°C)`) o
    sinónimo (`temperatura`). None si no se reconoce."""
    text = _normalize(raw or "")
    if not text:
        return None
    if text in KIND_ALIASES:
        return KIND_ALIASES[text]
    for value, label in MeterKind.choices:
        if _normalize(label) == text:
            return value
    first = text.split("_", 1)[0]  # «temperatura_c», «presion_bar»
    return KIND_ALIASES.get(first)


def reading_client_uuid(meter_id, read_at: datetime.datetime, value: Decimal) -> uuid.UUID:
    """`client_uuid` estable para una fila importada: misma lectura, mismo UUID (CBM-02 CA5)."""
    key = f"{meter_id}|{read_at.astimezone(datetime.UTC).isoformat()}|{value.normalize():f}"
    return uuid.uuid5(READING_NAMESPACE, key)


def parse_flag(raw) -> bool:
    if isinstance(raw, bool):
        return raw
    return _normalize(raw or "") in TRUE_VALUES


@dataclass
class ReadingRow:
    row: int
    asset_code: str
    meter_label: str
    value: str
    read_at: str  # ISO 8601
    is_reset: bool
    action: str  # create | duplicate | error
    meter_id: str = ""
    errors: list[str] = field(default_factory=list)

    @property
    def read_at_dt(self):
        return datetime.datetime.fromisoformat(self.read_at) if self.read_at else None


@dataclass
class ReadingPreview:
    rows: list[ReadingRow]

    @property
    def valid_rows(self):
        return [r for r in self.rows if r.action == "create"]

    def count(self, action):
        return sum(1 for r in self.rows if r.action == action)

    def to_session(self) -> dict:
        return {"rows": [asdict(r) for r in self.valid_rows]}


def _resolve_meters(raw_rows, membership):
    """Medidores por id y por código de activo, en dos consultas y dentro del alcance."""
    assets = visible_assets(membership=membership).filter(is_active=True)
    meter_ids = set()
    for row in raw_rows:
        try:
            meter_ids.add(uuid.UUID(str(row.get("meter") or "")))
        except ValueError:
            continue
    codes = {str(row.get("asset_code") or "").strip().lower() for row in raw_rows} - {""}
    code_filter = Q()
    for code in codes:
        code_filter |= Q(asset__code__iexact=code)
    by_id, by_code = {}, defaultdict(list)
    query = Q(pk__in=meter_ids) | code_filter if codes else Q(pk__in=meter_ids)
    for meter in Meter.objects.filter(query, is_active=True, asset__in=assets).select_related("asset"):
        by_id[str(meter.pk)] = meter
        by_code[meter.asset.code.lower()].append(meter)
    return by_id, by_code


def _pick_meter(row, candidates, *, kind, name, filtered):
    """El medidor del activo según `tipo` y `nombre` (opcionales); si no basta, un error amable."""
    if not candidates:
        row.errors.append(gettext("El activo no existe o no tiene medidores."))
        return None
    if kind:
        candidates = [m for m in candidates if m.kind == kind]
    if name:
        candidates = [m for m in candidates if m.name.lower() == name.lower()]
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        row.errors.append(gettext("El activo no tiene un medidor con ese tipo y nombre."))
    elif filtered:
        row.errors.append(
            gettext("El activo tiene varios medidores con ese tipo: indica también el nombre (o el id).")
        )
    else:
        row.errors.append(
            gettext("El activo tiene varios medidores: indica el tipo y el nombre, o el id del medidor.")
        )
    return None


def _existing_keys(meters, read_ats) -> set:
    if not meters or not read_ats:
        return set()
    return {
        (str(meter_id), read_at, value.normalize())
        for meter_id, read_at, value in MeterReading.objects.filter(
            meter__in=meters, read_at__in=read_ats
        ).values_list("meter_id", "read_at", "value")
    }


def build_reading_preview(raw_rows: list[dict], *, membership) -> ReadingPreview:
    """Valida cada fila sin escribir: medidor, valor, fecha, duplicados y retrocesos."""
    by_id, by_code = _resolve_meters(raw_rows, membership)
    now = timezone.now()
    rows, meters = [], {}
    for index, raw in enumerate(raw_rows, start=1):
        number = raw.get("_row", index)
        code = str(raw.get("asset_code") or "").strip()
        meter_ref = str(raw.get("meter") or "").strip()
        row = ReadingRow(
            row=number,
            asset_code=code,
            meter_label=meter_ref,
            value=str(raw.get("value") or "").strip(),
            read_at="",
            is_reset=parse_flag(raw.get("is_reset")),
            action="create",
        )
        meter = None
        kind_raw, name = str(raw.get("kind") or "").strip(), str(raw.get("name") or "").strip()
        kind = parse_kind(kind_raw)
        if kind_raw and kind is None:
            row.errors.append(gettext("Tipo de medidor desconocido: %(kind)s.") % {"kind": kind_raw})
        elif meter_ref:
            meter = by_id.get(meter_ref.lower())
            if meter is None or (code and meter.asset.code.lower() != code.lower()):
                row.errors.append(gettext("Medidor no encontrado."))
                meter = None
            elif (kind and meter.kind != kind) or (name and meter.name.lower() != name.lower()):
                row.errors.append(gettext("El id del medidor no coincide con el tipo o el nombre."))
                meter = None
        elif code:
            meter = _pick_meter(
                row, by_code.get(code.lower(), []), kind=kind, name=name, filtered=bool(kind_raw or name)
            )
        else:
            row.errors.append(gettext("Falta el código del activo o el id del medidor."))
        if meter is not None:
            row.meter_id, row.asset_code = str(meter.pk), meter.asset.code
            row.meter_label = f"{meter.name} ({meter.unit})" if meter.name else meter.get_kind_display()
            meters[row.meter_id] = meter
        try:
            value = parse_value(raw.get("value") if raw.get("value") is not None else "")
        except ValueError:
            row.errors.append(gettext("Valor inválido."))
        else:
            if value < 0 and not (meter is not None and meter.is_variable):  # CBM-01: bajo cero vale
                row.errors.append(gettext("La lectura no puede ser negativa."))
            row.value = f"{value.normalize():f}"
        try:
            read_at = parse_read_at(raw.get("read_at") or "")
        except (ValueError, TypeError):
            row.errors.append(gettext("Fecha inválida: usa aaaa-mm-dd hh:mm o dd-mm-aaaa hh:mm."))
        else:
            if read_at > now + FUTURE_TOLERANCE:
                row.errors.append(gettext("La fecha está en el futuro."))
            row.read_at = read_at.isoformat()
        rows.append(row)

    valid = [r for r in rows if not r.errors]
    existing = _existing_keys(list(meters.values()), [r.read_at_dt for r in valid])
    seen = set()
    for row in valid:
        key = (row.meter_id, row.read_at_dt, Decimal(row.value).normalize())
        if key in existing or key in seen:
            row.action = "duplicate"
        seen.add(key)
    _check_cumulative([r for r in valid if r.action == "create"], meters)
    for row in rows:
        if row.errors:
            row.action = "error"
    return ReadingPreview(rows=rows)


def _check_cumulative(rows, meters):
    """Simula la secuencia por medidor: una acumulativa no baja respecto de la vigente sin reinicio."""
    by_meter = defaultdict(list)
    for row in rows:
        by_meter[row.meter_id].append(row)
    for meter_id, meter_rows in by_meter.items():
        meter = meters[meter_id]
        if not meter.is_cumulative:
            continue
        current, current_at = meter.current_value, meter.current_read_at
        for row in sorted(meter_rows, key=lambda r: r.read_at_dt):
            value, at = Decimal(row.value), row.read_at_dt
            if current_at is not None and at < current_at:
                continue  # lectura histórica: no cambia la vigente
            if current is not None and value < current and not row.is_reset:
                row.errors.append(
                    gettext(
                        "La lectura (%(value)s) es menor que la anterior (%(previous)s). "
                        "Si se cambió o reinició el medidor, márcalo."
                    )
                    % {"value": row.value, "previous": f"{current.normalize():f}"}
                )
                continue
            current, current_at = value, at


@dataclass
class ReadingImportResult:
    created: int = 0
    duplicates: int = 0
    errors: list[tuple[int, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    readings: list = field(default_factory=list)


def apply_reading_import(rows: list[dict], *, membership, source: str) -> ReadingImportResult:
    """Guarda las filas válidas de una vista previa (en orden de fecha, cada una en su transacción).

    Un `meter_reading.created` por lectura, despachados en un solo lote al final (X-08).
    """
    from apps.webhooks.services import collect_events

    with collect_events():
        return _apply_reading_import(rows, membership=membership, source=source)


def _apply_reading_import(rows: list[dict], *, membership, source: str) -> ReadingImportResult:
    result = ReadingImportResult()
    meters = {
        str(m.pk): m
        for m in Meter.objects.filter(
            pk__in={r["meter_id"] for r in rows},
            is_active=True,
            asset__in=visible_assets(membership=membership),
        ).select_related("asset")
    }
    touched = {}
    for row in sorted(rows, key=lambda r: (r["meter_id"], r["read_at"])):
        meter = meters.get(row["meter_id"])
        if meter is None:
            result.errors.append((row["row"], gettext("Medidor no encontrado.")))
            continue
        value, read_at = Decimal(row["value"]), datetime.datetime.fromisoformat(row["read_at"])
        if MeterReading.objects.filter(meter=meter, read_at=read_at, value=value).exists():
            result.duplicates += 1
            continue
        try:
            reading, warnings = record_meter_reading(
                meter=meter,
                value=value,
                membership=membership,
                read_at=read_at,
                is_reset=row["is_reset"],
                source=source,
                client_uuid=reading_client_uuid(meter.pk, read_at, value),
                generate_work_orders=False,  # una vez por medidor, al final
            )
        except MeterReadingError as error:
            result.errors.append((row["row"], error.message))
            continue
        result.created += 1
        result.readings.append(reading)
        result.warnings += [f"{meter.asset.code}: {w}" for w in warnings]
        touched[meter.pk] = meter
    from apps.maintenance.services import generate_due_work_orders

    for meter in touched.values():
        meter.refresh_from_db()
        generate_due_work_orders(meter=meter)
    return result
