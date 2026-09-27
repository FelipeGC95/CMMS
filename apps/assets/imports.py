"""Importación de activos desde Excel (AST-08).

Flujo en dos pasos: `parse_workbook` + `build_preview` validan sin escribir
nada; `apply_import` guarda las filas válidas en una transacción. La
importación es idempotente por código de activo: si el código existe se
actualiza (las celdas vacías no borran datos). Clases y ubicaciones que no
existen se crean solas, para que la carga inicial no tenga pasos previos.
"""

import datetime
import io
import re
import unicodedata
import zipfile
from dataclasses import asdict, dataclass, field
from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext, gettext_lazy
from openpyxl import Workbook, load_workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.utils.exceptions import InvalidFileException
from openpyxl.worksheet.datavalidation import DataValidation

from apps.accounts import scoping
from apps.organizations.models import Location

from .models import Asset, AssetClass, Meter, MeterKind
from .services import record_meter_reading

MAX_ROWS = 2000
MAX_FILE_BYTES = 5 * 1024 * 1024
SHEET_NAME = gettext_lazy("Activos")

# (clave interna, encabezado de la plantilla, obligatorio, ayuda)
COLUMNS = [
    (
        "code",
        gettext_lazy("Código"),
        True,
        gettext_lazy("Único por activo. Si ya existe, la fila actualiza ese activo."),
    ),
    ("name", gettext_lazy("Nombre"), True, gettext_lazy("Ej: Grúa horquilla eléctrica 2,5 t")),
    (
        "asset_class",
        gettext_lazy("Clase"),
        True,
        gettext_lazy("Nombre o código de la clase. Si no existe, se crea."),
    ),
    (
        "location",
        gettext_lazy("Ubicación"),
        False,
        gettext_lazy("Nombre o código del sitio/área. Si no existe, se crea como sitio."),
    ),
    (
        "parent",
        gettext_lazy("Activo padre"),
        False,
        gettext_lazy("Código del activo del que es parte (puede estar en el mismo archivo)."),
    ),
    (
        "criticality",
        gettext_lazy("Criticidad"),
        False,
        gettext_lazy("A (alta), B (media) o C (baja). Por defecto B."),
    ),
    ("manufacturer", gettext_lazy("Fabricante"), False, ""),
    ("model", gettext_lazy("Modelo"), False, ""),
    ("serial_number", gettext_lazy("N° de serie"), False, ""),
    ("year", gettext_lazy("Año"), False, gettext_lazy("Año de fabricación, ej: 2019.")),
    ("meter_kind", gettext_lazy("Medidor"), False, gettext_lazy("Horómetro, Odómetro o Ciclos (opcional).")),
    (
        "meter_value",
        gettext_lazy("Lectura actual"),
        False,
        gettext_lazy("Lectura del medidor hoy (opcional)."),
    ),
]
HEADER_ALIASES = {
    "codigo": "code",
    "code": "code",
    "nombre": "name",
    "clase": "asset_class",
    "clase_de_activo": "asset_class",
    "tipo": "asset_class",
    "ubicacion": "location",
    "sitio": "location",
    "activo_padre": "parent",
    "padre": "parent",
    "criticidad": "criticality",
    "fabricante": "manufacturer",
    "marca": "manufacturer",
    "modelo": "model",
    "n_de_serie": "serial_number",
    "no_de_serie": "serial_number",
    "numero_de_serie": "serial_number",
    "serie": "serial_number",
    "ano": "year",
    "anio": "year",
    "medidor": "meter_kind",
    "lectura_actual": "meter_value",
    "lectura": "meter_value",
}
# Valores de entrada reconocidos (el texto de las celdas es dato, no interfaz): al
# traducir la plantilla (F3) se agregan aquí los alias del otro idioma.
CRITICALITY_ALIASES = {"a": "A", "alta": "A", "b": "B", "media": "B", "c": "C", "baja": "C"}
METER_ALIASES = {
    "horometro": MeterKind.HOURS,
    "horas": MeterKind.HOURS,
    "h": MeterKind.HOURS,
    "odometro": MeterKind.KM,
    "km": MeterKind.KM,
    "kilometraje": MeterKind.KM,
    "ciclos": MeterKind.CYCLES,
}


class ImportFileError(ValidationError):
    """El archivo no se puede leer (no las filas: esas se informan una a una)."""


def _normalize(text) -> str:
    text = unicodedata.normalize("NFKD", str(text)).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def _cell_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return str(value).strip()


# --- Lectura del archivo ----------------------------------------------------


def parse_workbook(uploaded_file) -> list[dict]:
    """Lee la primera hoja con encabezados reconocibles. Devuelve filas como texto."""
    if uploaded_file.size > MAX_FILE_BYTES:
        raise ImportFileError(gettext("El archivo supera 5 MB."))
    try:
        workbook = load_workbook(uploaded_file, read_only=True, data_only=True)
    except (InvalidFileException, zipfile.BadZipFile, KeyError, OSError, ValueError) as exc:
        raise ImportFileError(
            gettext("No se pudo leer el archivo. Usa un Excel .xlsx (idealmente la plantilla).")
        ) from exc
    sheet_name = str(SHEET_NAME)
    sheet = workbook[sheet_name] if sheet_name in workbook.sheetnames else workbook.worksheets[0]
    rows = sheet.iter_rows(values_only=True)
    header = next(rows, None) or ()
    keys = [HEADER_ALIASES.get(_normalize(h)) if h is not None else None for h in header]
    missing = [label for key, label, required, _ in COLUMNS if required and key not in keys]
    if missing:
        raise ImportFileError(
            gettext("Faltan columnas obligatorias: %(columns)s.") % {"columns": ", ".join(map(str, missing))}
        )

    parsed = []
    for number, values in enumerate(rows, start=2):
        row = {key: _cell_text(v) for key, v in zip(keys, values, strict=False) if key}
        if not any(row.values()):
            continue
        if len(parsed) >= MAX_ROWS:
            raise ImportFileError(
                gettext("El archivo tiene más de %(max)s filas; divídelo en partes.") % {"max": MAX_ROWS}
            )
        row["_row"] = number
        parsed.append(row)
    workbook.close()
    if not parsed:
        raise ImportFileError(gettext("El archivo no tiene filas con datos."))
    return parsed


# --- Alcance por sitio -----------------------------------------------------


def allowed_location_ids(membership):
    """Único punto de conexión con el alcance por sitio (ORG-06).

    None = sin restricción; si no, el conjunto de ubicaciones permitidas
    (incluye descendientes). Un usuario limitado no puede importar filas sin
    ubicación: esos activos quedarían fuera de su vista.
    """
    return scoping.allowed_location_ids(membership)


# --- Validación (sin escribir) ---------------------------------------------


@dataclass
class RowResult:
    row: int
    code: str
    name: str
    action: str  # create | update | error
    errors: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    data: dict = field(default_factory=dict)
    new_class: str = ""
    new_location: str = ""


@dataclass
class Preview:
    rows: list[RowResult]
    new_classes: list[str]
    new_locations: list[str]

    @property
    def valid_rows(self):
        return [r for r in self.rows if r.action != "error"]

    def count(self, action):
        return sum(1 for r in self.rows if r.action == action)

    def to_session(self) -> dict:
        return {
            "rows": [asdict(r) for r in self.valid_rows],
            "new_classes": self.new_classes,
            "new_locations": self.new_locations,
        }


def _match(value: str, index: dict):
    return index.get(value.lower())


def _index(queryset):
    index = {}
    for obj in queryset:
        index.setdefault(obj.code.lower(), obj)
        index.setdefault(obj.name.lower(), obj)
    return index


def build_preview(raw_rows: list[dict], *, membership=None) -> Preview:
    allowed = allowed_location_ids(membership) if membership is not None else None
    classes = _index(AssetClass.objects.filter(is_active=True))
    locations = _index(Location.objects.filter(is_active=True))
    existing = {a.code.lower(): a for a in Asset.objects.prefetch_related("meters")}
    file_codes = {r.get("code", "").lower() for r in raw_rows if r.get("code")}
    seen = set()
    results = []
    this_year = timezone.localdate().year

    for raw in raw_rows:
        code, name = raw.get("code", ""), raw.get("name", "")
        result = RowResult(row=raw["_row"], code=code, name=name, action="create")
        errors, notes, data = result.errors, result.notes, result.data

        if not code:
            errors.append(gettext("Falta el código."))
        elif len(code) > 30:
            errors.append(gettext("El código tiene más de 30 caracteres."))
        elif code.lower() in seen:
            errors.append(gettext("Código repetido en el archivo."))
        seen.add(code.lower())
        current = existing.get(code.lower())
        if current:
            result.action = "update"
        if not name and not current:
            errors.append(gettext("Falta el nombre."))
        elif len(name) > 150:
            errors.append(gettext("El nombre tiene más de 150 caracteres."))

        class_value = raw.get("asset_class", "")
        if class_value:
            if len(class_value) > 120:
                errors.append(gettext("La clase tiene más de 120 caracteres."))
            elif not _match(class_value, classes):
                result.new_class = class_value
                notes.append(gettext("Clase nueva: %(name)s") % {"name": class_value})
        elif not current:
            errors.append(gettext("Falta la clase."))

        location_value = raw.get("location", "")
        if location_value and not _match(location_value, locations):
            if len(location_value) > 150:
                errors.append(gettext("La ubicación tiene más de 150 caracteres."))
            else:
                result.new_location = location_value
                notes.append(gettext("Ubicación nueva: %(name)s") % {"name": location_value})

        if allowed is not None:
            target = _match(location_value, locations) if location_value else None
            if location_value and target is None:
                errors.append(
                    gettext("Tu acceso está limitado a ciertos sitios: no puedes crear ubicaciones.")
                )
            elif target is not None and target.id not in allowed:
                errors.append(
                    gettext("«%(name)s» está fuera de los sitios a los que tienes acceso.")
                    % {"name": location_value}
                )
            elif target is None and not current:
                errors.append(gettext("Indica una ubicación de los sitios a los que tienes acceso."))
            if current and current.location_id not in allowed:
                errors.append(gettext("El activo existente está en un sitio al que no tienes acceso."))

        parent = raw.get("parent", "")
        if parent:
            if parent.lower() == code.lower():
                errors.append(gettext("Un activo no puede ser padre de sí mismo."))
            elif parent.lower() not in existing and parent.lower() not in file_codes:
                errors.append(gettext("No existe el activo padre «%(code)s».") % {"code": parent})

        criticality = raw.get("criticality", "")
        if criticality:
            data["criticality"] = CRITICALITY_ALIASES.get(_normalize(criticality))
            if not data["criticality"]:
                errors.append(gettext("Criticidad inválida: usa A, B o C."))

        year = raw.get("year", "")
        if year:
            if not year.isdigit() or not 1900 <= int(year) <= this_year + 1:
                errors.append(gettext("Año inválido."))

        meter_kind, meter_value = raw.get("meter_kind", ""), raw.get("meter_value", "")
        if meter_kind:
            data["meter_kind"] = METER_ALIASES.get(_normalize(meter_kind))
            if not data["meter_kind"]:
                errors.append(gettext("Medidor inválido: usa Horómetro, Odómetro o Ciclos."))
        if meter_value:
            try:
                value = Decimal(meter_value.replace(",", "."))
                if value < 0:
                    raise InvalidOperation
            except InvalidOperation:
                errors.append(gettext("La lectura debe ser un número mayor o igual a cero."))
            else:
                if not meter_kind:
                    errors.append(gettext("Indica el medidor de la lectura."))
                elif current and data.get("meter_kind"):
                    meter = next((m for m in current.meters.all() if m.kind == data["meter_kind"]), None)
                    if meter and meter.current_value is not None and value < meter.current_value:
                        notes.append(
                            gettext("Lectura menor que la actual (%(value)s): se omitirá.")
                            % {"value": f"{meter.current_value.normalize():f}"}
                        )
                        meter_value = ""

        for key in ("manufacturer", "model", "serial_number"):
            if len(raw.get(key, "")) > 100:
                errors.append(
                    gettext("%(column)s: máximo 100 caracteres.")
                    % {"column": dict((k, lbl) for k, lbl, *_ in COLUMNS)[key]}
                )

        if errors:
            result.action = "error"
        # Los valores normalizados (criticidad, medidor) prevalecen sobre el texto original.
        normalized = {k: data[k] for k in ("criticality", "meter_kind") if data.get(k)}
        data.update({k: v for k, v in raw.items() if k != "_row"})
        data.update(normalized, meter_value=meter_value)
        results.append(result)

    # Solo se crean las clases y ubicaciones que usan filas válidas.
    valid = [r for r in results if r.action != "error"]
    new_classes = {r.new_class.lower(): r.new_class for r in valid if r.new_class}
    new_locations = {r.new_location.lower(): r.new_location for r in valid if r.new_location}
    return Preview(results, sorted(new_classes.values()), sorted(new_locations.values()))


# --- Aplicación -------------------------------------------------------------


@dataclass
class ImportResult:
    created: int = 0
    updated: int = 0
    classes_created: int = 0
    locations_created: int = 0
    plans_assigned: int = 0
    readings: int = 0
    warnings: list[str] = field(default_factory=list)


def _unique_code(base: str, taken: set[str], max_length: int = 30) -> str:
    base = (_normalize(base).upper().replace("_", "-") or "X")[: max_length - 4]
    code, n = base, 2
    while code.lower() in taken:
        code = f"{base}-{n}"
        n += 1
    taken.add(code.lower())
    return code


@transaction.atomic
def apply_import(session_data: dict, *, assign_class_plans: bool = True) -> ImportResult:
    """Guarda las filas válidas de la vista previa. Todo o nada."""
    from apps.maintenance.services import assign_class_plans as assign_plans_by_class

    result = ImportResult()
    classes = _index(AssetClass.objects.all())
    locations = _index(Location.objects.all())
    class_codes = {c.code.lower() for c in AssetClass.objects.all()}
    location_codes = {loc.code.lower() for loc in Location.objects.all()}

    for name in session_data["new_classes"]:
        if not _match(name, classes):
            obj = AssetClass.objects.create(code=_unique_code(name, class_codes), name=name[:120])
            classes[obj.code.lower()] = classes[obj.name.lower()] = obj
            result.classes_created += 1
    for name in session_data["new_locations"]:
        if not _match(name, locations):
            obj = Location.objects.create(code=_unique_code(name, location_codes), name=name[:150])
            locations[obj.code.lower()] = locations[obj.name.lower()] = obj
            result.locations_created += 1

    existing = {a.code.lower(): a for a in Asset.objects.all()}
    touched, new_assets = {}, []
    for row in session_data["rows"]:
        data = row["data"]
        asset = existing.get(data["code"].lower())
        is_new = asset is None
        if is_new:
            asset = Asset(code=data["code"])
        if data.get("name"):
            asset.name = data["name"]
        if data.get("asset_class"):
            asset.asset_class = _match(data["asset_class"], classes)
        if data.get("location"):
            asset.location = _match(data["location"], locations)
        if data.get("criticality"):
            asset.criticality = data["criticality"]
        for key in ("manufacturer", "model", "serial_number"):
            if data.get(key):
                setattr(asset, key, data[key])
        if data.get("year"):
            asset.year = int(data["year"])
        asset.save()
        existing[asset.code.lower()] = touched[asset.code.lower()] = asset
        if is_new:
            result.created += 1
            new_assets.append(asset)
        else:
            result.updated += 1

    # Segunda pasada: padres (pueden venir en el mismo archivo) y medidores.
    for row in session_data["rows"]:
        data = row["data"]
        asset = touched[data["code"].lower()]
        if data.get("parent"):
            parent = existing[data["parent"].lower()]
            if _creates_cycle(asset, parent):
                result.warnings.append(
                    gettext("Fila %(row)s: se omitió el padre para evitar un ciclo.") % {"row": row["row"]}
                )
            else:
                asset.parent = parent
                asset.save(update_fields=["parent", "updated_at"])
        if data.get("meter_kind"):
            meter, _ = Meter.objects.get_or_create(asset=asset, kind=data["meter_kind"], name="")
            if data.get("meter_value"):
                record_meter_reading(
                    meter=meter,
                    value=Decimal(data["meter_value"].replace(",", ".")),
                    source="import",
                    generate_work_orders=False,
                )
                result.readings += 1

    if assign_class_plans and new_assets:
        assigned, warnings = assign_plans_by_class(assets=new_assets)
        result.plans_assigned += assigned
        result.warnings.extend(warnings)
    return result


def _creates_cycle(asset: Asset, parent: Asset) -> bool:
    node, steps = parent, 0
    while node is not None and steps < 50:
        if node.pk == asset.pk:
            return True
        node, steps = node.parent, steps + 1
    return False


# --- Plantilla --------------------------------------------------------------


def build_template() -> bytes:
    """Plantilla .xlsx con encabezados, un ejemplo, listas desplegables e instrucciones."""
    wb = Workbook()
    ws = wb.active
    ws.title = str(SHEET_NAME)
    header_fill = PatternFill("solid", fgColor="0B2E4F")
    for col, (_key, label, _required, _help) in enumerate(COLUMNS, start=1):
        cell = ws.cell(row=1, column=col, value=str(label))
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = header_fill
        ws.column_dimensions[cell.column_letter].width = max(14, len(label) + 6)
    example = [
        "GRH-01",
        gettext("Grúa horquilla eléctrica 2,5 t"),
        gettext("Grúa horquilla"),
        gettext("Bodega"),
        "",
        "B",
        "Toyota",
        "8FBE25",
        "ABC123",
        2019,
        "Horómetro",
        4200,
    ]
    for col, value in enumerate(example, start=1):
        ws.cell(row=2, column=col, value=value).font = Font(italic=True, color="475569")
    ws.freeze_panes = "A2"

    def dropdown(options, column_key):
        col = [k for k, *_ in COLUMNS].index(column_key) + 1
        letter = ws.cell(row=1, column=col).column_letter
        validation = DataValidation(type="list", formula1=f'"{",".join(options)}"', allow_blank=True)
        validation.add(f"{letter}2:{letter}{MAX_ROWS + 1}")
        ws.add_data_validation(validation)

    dropdown(["A", "B", "C"], "criticality")
    dropdown(["Horómetro", "Odómetro", "Ciclos"], "meter_kind")

    help_sheet = wb.create_sheet(gettext("Instrucciones"))
    help_sheet.column_dimensions["A"].width = 18
    help_sheet.column_dimensions["B"].width = 90
    help_sheet.append([gettext("Columna"), gettext("Cómo llenarla")])
    for cell in help_sheet[1]:
        cell.font = Font(bold=True)
    for _, label, required, help_text in COLUMNS:
        column = gettext("%(column)s (obligatoria)") % {"column": label} if required else str(label)
        help_sheet.append([column, str(help_text)])
    help_sheet.append([])
    help_sheet.append(["", gettext("Borra la fila de ejemplo (en cursiva) antes de subir el archivo.")])
    help_sheet.append(
        [
            "",
            gettext("Máximo %(max)s filas por archivo. Antes de guardar verás una vista previa.")
            % {"max": MAX_ROWS},
        ]
    )

    reference = wb.create_sheet(gettext("Clases y ubicaciones"))
    reference.append([gettext("Clases existentes"), gettext("Ubicaciones existentes")])
    for cell in reference[1]:
        cell.font = Font(bold=True)
    classes = list(AssetClass.objects.filter(is_active=True).values_list("name", flat=True))
    places = list(Location.objects.filter(is_active=True).values_list("name", flat=True))
    for i in range(max(len(classes), len(places))):
        reference.append([classes[i] if i < len(classes) else None, places[i] if i < len(places) else None])
    reference.column_dimensions["A"].width = 32
    reference.column_dimensions["B"].width = 32

    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


def template_filename() -> str:
    return f"plantilla-activos-{datetime.date.today():%Y%m%d}.xlsx"
