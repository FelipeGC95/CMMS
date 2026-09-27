"""Exportación de listas a Excel (KPI-03) con openpyxl en modo de solo escritura.

Cada exportación respeta los filtros de su lista y el alcance por sitio: recibe
la consulta ya filtrada por el selector de la lista.
"""

import io

from django.utils import timezone
from django.utils.translation import gettext as _
from openpyxl import Workbook
from openpyxl.cell import WriteOnlyCell
from openpyxl.styles import Font

from apps.ui.templatetags.ui import work_order_status_label

# Tope de filas por archivo: por sobre esto se pide filtrar la lista (el archivo se arma en memoria).
MAX_EXPORT_ROWS = 10_000

XLSX_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _local(value):
    """Excel no admite zonas horarias: fecha y hora locales, sin zona."""
    return timezone.localtime(value).replace(tzinfo=None) if value else None


def _workbook(title, headers, rows) -> bytes:
    workbook = Workbook(write_only=True)
    sheet = workbook.create_sheet(title=title[:31])
    sheet.freeze_panes = "A2"
    bold = Font(bold=True)
    header_cells = []
    for header in headers:
        cell = WriteOnlyCell(sheet, value=str(header))
        cell.font = bold
        header_cells.append(cell)
    sheet.append(header_cells)
    for row in rows:
        sheet.append(row)
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def assets_xlsx(assets) -> bytes:
    """`assets`: consulta de la lista de activos (ya filtrada y con alcance)."""
    headers = [
        _("Código"),
        _("Nombre"),
        _("Clase"),
        _("Ubicación"),
        _("Activo padre"),
        _("Estado"),
        _("Criticidad"),
        _("Fabricante"),
        _("Modelo"),
        _("N.º de serie"),
        _("Año"),
        _("Puesta en servicio"),
    ]
    rows = (
        (
            a.code,
            a.name,
            a.asset_class.name,
            a.location.name if a.location else "",
            a.parent.code if a.parent else "",
            a.get_status_display(),
            a.criticality,
            a.manufacturer,
            a.model,
            a.serial_number,
            a.year,
            a.in_service_on,
        )
        for a in assets.select_related("asset_class", "location", "parent").order_by("code").iterator()
    )
    return _workbook(_("Activos"), headers, rows)


def work_orders_xlsx(work_orders) -> bytes:
    """`work_orders`: consulta de la lista de OT (ya filtrada y con alcance), en su orden."""
    headers = [
        _("OT"),
        _("Tipo"),
        _("Estado"),
        _("Prioridad"),
        _("Título"),
        _("Activo o ubicación"),
        _("Responsable"),
        _("Fecha límite"),
        _("Creada"),
        _("Completada"),
        _("Cerrada"),
        _("Costo total"),
    ]
    rows = (
        (
            wo.code,
            wo.get_work_type_display(),
            str(work_order_status_label(wo.status)),
            wo.get_priority_display(),
            wo.title,
            str(wo.asset.code if wo.asset_id else wo.location.name if wo.location_id else ""),
            (wo.assigned_to.full_name or wo.assigned_to.email) if wo.assigned_to_id else "",
            wo.due_on,
            _local(wo.created_at),
            _local(wo.completed_at),
            _local(wo.closed_at),
            wo.total_cost,
        )
        for wo in work_orders.select_related("asset", "location", "assigned_to").iterator()
    )
    return _workbook(_("Órdenes de trabajo"), headers, rows)


def backlog_xlsx(work_orders) -> bytes:
    """`work_orders`: backlog filtrado (REQ-04), anotado con `score` y en su orden."""
    headers = [
        _("Puntaje"),
        _("OT"),
        _("Tipo"),
        _("Estado"),
        _("Prioridad"),
        _("Criticidad"),
        _("Título"),
        _("Activo o ubicación"),
        _("Responsable"),
        _("Fecha límite"),
        _("Programada para"),
        _("Estimación (h)"),
    ]
    rows = (
        (
            wo.score,
            wo.code,
            wo.get_work_type_display(),
            str(work_order_status_label(wo.status)),
            wo.get_priority_display(),
            wo.asset.criticality if wo.asset_id else "",
            wo.title,
            str(wo.asset.code if wo.asset_id else wo.location.name if wo.location_id else ""),
            (wo.assigned_to.full_name or wo.assigned_to.email) if wo.assigned_to_id else "",
            wo.due_on,
            wo.scheduled_for,
            round(wo.estimated_minutes / 60, 1) if wo.estimated_minutes else None,
        )
        for wo in work_orders.iterator()
    )
    return _workbook(_("Backlog"), headers, rows)
