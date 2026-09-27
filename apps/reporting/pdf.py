"""Ficha de activo y OT en PDF con la marca de la organización (KPI-03, ADR-019).

reportlab (platypus) sobre A4 con las fuentes DejaVu incrustadas de `apps.core.pdf`.
Cada página lleva el logo de la organización (o su nombre), el título del
documento y el pie con la fecha de generación y el número de página.
"""

import io
from xml.sax.saxutils import escape

from django.utils import timezone
from django.utils.formats import date_format
from django.utils.translation import gettext as _
from reportlab.lib.colors import HexColor
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.platypus import Image, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from apps.core.pdf import BOLD_FONT, TEXT_FONT, read_logo, register_fonts
from apps.ui.templatetags.ui import num, work_order_status_label

# Colores de impresión (un PDF no usa los tokens CSS): tinta de la marca sobre blanco.
INK = HexColor("#0B2E4F")
MUTED = HexColor("#4A5B6C")
RULE = HexColor("#B8C2CC")
HEADER_FILL = HexColor("#EEF2F5")

MARGIN = 15 * mm
HEADER_H = 16 * mm
CONTENT_W = A4[0] - 2 * MARGIN

TEXT = ParagraphStyle("text", fontName=TEXT_FONT, fontSize=9, leading=12, textColor=INK)
SMALL = ParagraphStyle("small", parent=TEXT, fontSize=8, leading=10)
LABEL = ParagraphStyle("label", parent=SMALL, textColor=MUTED)
TH = ParagraphStyle("th", parent=SMALL, fontName=BOLD_FONT)
H1 = ParagraphStyle("h1", parent=TEXT, fontName=BOLD_FONT, fontSize=16, leading=20, spaceAfter=2 * mm)
H2 = ParagraphStyle("h2", parent=TEXT, fontName=BOLD_FONT, fontSize=11, leading=14, spaceBefore=5 * mm)
SUBTITLE = ParagraphStyle("subtitle", parent=TEXT, textColor=MUTED, spaceAfter=3 * mm)

DASH = "—"


def _p(value, style=TEXT):
    text = DASH if value is None or value == "" else str(value)
    return Paragraph(escape(text).replace("\n", "<br/>"), style)


def _dt(value):
    return date_format(timezone.localtime(value), "d M Y H:i") if value else DASH


def _d(value):
    return date_format(value, "d M Y") if value else DASH


def _money(value):
    return f"$ {num(round(value or 0, 2))}"


def _pairs(rows):
    """Tabla de dos pares (etiqueta: valor) por fila: aprovecha el ancho de la hoja."""
    cells, line = [], []
    for label, value in rows:
        line += [_p(label, LABEL), _p(value)]
        if len(line) == 4:
            cells.append(line)
            line = []
    if line:
        cells.append(line + [""] * (4 - len(line)))
    quarter = CONTENT_W / 4
    table = Table(cells, colWidths=[quarter * 0.7, quarter * 1.3] * 2)
    table.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LINEBELOW", (0, 0), (-1, -1), 0.3, RULE),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ]
        )
    )
    return table


def _grid(headers, rows, widths, empty):
    """Tabla con encabezado repetido en cada hoja; `widths` en fracciones del ancho útil."""
    if not rows:
        return _p(empty, LABEL)
    data = [[_p(h, TH) for h in headers]] + [[_p(v, SMALL) for v in row] for row in rows]
    table = Table(data, colWidths=[CONTENT_W * w for w in widths], repeatRows=1)
    table.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("BACKGROUND", (0, 0), (-1, 0), HEADER_FILL),
                ("LINEBELOW", (0, 0), (-1, -1), 0.3, RULE),
                ("TOPPADDING", (0, 0), (-1, -1), 2),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ]
        )
    )
    return table


def _render(*, organization, title, subtitle, story) -> bytes:
    register_fonts()
    logo = read_logo(organization)
    generated = _dt(timezone.now())
    buffer = io.BytesIO()

    def decorate(canvas, doc):
        canvas.saveState()
        top = A4[1] - MARGIN
        if logo is not None:
            canvas.drawImage(
                logo,
                MARGIN,
                top - HEADER_H + 4 * mm,
                width=50 * mm,
                height=HEADER_H - 4 * mm,
                preserveAspectRatio=True,
                anchor="w",
                mask="auto",
            )
        else:
            canvas.setFont(BOLD_FONT, 12)
            canvas.setFillColor(INK)
            canvas.drawString(MARGIN, top - 8 * mm, organization.name)
        canvas.setFont(TEXT_FONT, 8)
        canvas.setFillColor(MUTED)
        canvas.drawRightString(A4[0] - MARGIN, top - 8 * mm, title)
        canvas.setStrokeColor(RULE)
        canvas.line(MARGIN, top - HEADER_H + 2 * mm, A4[0] - MARGIN, top - HEADER_H + 2 * mm)
        footer = _("%(org)s · generado el %(date)s · página %(page)s") % {
            "org": organization.name,
            "date": generated,
            "page": doc.page,
        }
        canvas.drawString(MARGIN, MARGIN / 2, footer)
        canvas.restoreState()

    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=MARGIN,
        rightMargin=MARGIN,
        topMargin=MARGIN + HEADER_H,
        bottomMargin=MARGIN,
        title=title,
        author=organization.name,
        creator="GALBUST CMMS",
    )
    doc.build([_p(title, H1), _p(subtitle, SUBTITLE), *story], onFirstPage=decorate, onLaterPages=decorate)
    return buffer.getvalue()


# --- Ficha de activo -------------------------------------------------------------


def asset_sheet_pdf(*, organization, asset, attributes, meters, assignments, work_orders, failures) -> bytes:
    """Ficha del activo: datos, datos técnicos, medidores, planes, OT recientes y fallas."""
    register_fonts()  # antes de armar los párrafos: platypus resuelve la fuente al crearlos
    data = [
        (_("Código"), asset.code),
        (_("Clase"), asset.asset_class.name),
        (_("Ubicación"), asset.location or ""),
        (_("Activo padre"), asset.parent or ""),
        (_("Estado"), asset.get_status_display()),
        (_("Criticidad"), asset.get_criticality_display()),
        (_("Fabricante"), asset.manufacturer),
        (_("Modelo"), asset.model),
        (_("N.º de serie"), asset.serial_number),
        (_("Año"), asset.year or ""),
        (_("Puesta en servicio"), _d(asset.in_service_on)),
    ]
    story = [_pairs(data)]
    if attributes:
        story += [
            _p(_("Datos técnicos"), H2),
            _pairs([(f"{a.label} ({a.unit})" if a.unit else a.label, text) for a, text in attributes]),
        ]
    story += [
        _p(_("Medidores"), H2),
        _grid(
            [_("Medidor"), _("Valor actual"), _("Última lectura")],
            [
                (m.name or m.get_kind_display(), f"{num(m.current_value)} {m.unit}", _dt(m.current_read_at))
                for m in meters
            ],
            [0.5, 0.25, 0.25],
            _("Sin medidores."),
        ),
        _p(_("Planes preventivos"), H2),
        _grid(
            [_("Plan"), _("Próximo vencimiento"), _("Última ejecución")],
            [(a.plan.name, _d(a.next_due_on), _d(a.last_done_on)) for a in assignments],
            [0.5, 0.25, 0.25],
            _("Sin planes asignados."),
        ),
        _p(_("OT recientes"), H2),
        _grid(
            [_("OT"), _("Título"), _("Estado"), _("Creada"), _("Cerrada")],
            [
                (wo.code, wo.title, work_order_status_label(wo.status), _d(wo.created_at), _d(wo.closed_at))
                for wo in work_orders
            ],
            [0.14, 0.4, 0.16, 0.15, 0.15],
            _("Sin OT."),
        ),
        _p(_("Fallas recientes"), H2),
        _grid(
            [_("Inicio"), _("Término"), _("Impacto"), _("Modo de falla"), _("OT")],
            [
                (
                    _dt(f.started_at),
                    _dt(f.ended_at),
                    f.get_impact_display(),
                    f.failure_mode.name if f.failure_mode else "",
                    f.work_order.code if f.work_order else "",
                )
                for f in failures
            ],
            [0.2, 0.2, 0.18, 0.28, 0.14],
            _("Sin fallas registradas."),
        ),
    ]
    return _render(
        organization=organization,
        title=_("Ficha de activo %(code)s") % {"code": asset.code},
        subtitle=asset.name,
        story=story,
    )


# --- OT ----------------------------------------------------------------------------


def _signature_image(signature):
    if signature is None:
        return None
    try:
        with signature.file.open("rb") as stored:
            content = stored.read()
        width, height = ImageReader(io.BytesIO(content)).getSize()
    except (OSError, ValueError):
        return None  # archivo perdido o ilegible: la OT se imprime sin firma
    if not width or not height:
        return None
    scale = min(60 * mm / width, 30 * mm / height)
    return Image(io.BytesIO(content), width=width * scale, height=height * scale, hAlign="LEFT")


def _checklist_value(item):
    if item.value_number is not None:
        return f"{num(item.value_number)} {item.unit}".strip()
    return ""


def work_order_pdf(*, organization, work_order, helpers, checklist, signature) -> bytes:
    """OT: encabezado, checklist con resultados, costos y firma de conformidad."""
    register_fonts()
    wo = work_order
    target = wo.asset or wo.location
    data = [
        (_("Tipo"), wo.get_work_type_display()),
        (_("Estado"), work_order_status_label(wo.status)),
        (_("Prioridad"), wo.get_priority_display()),
        (_("Activo o ubicación"), target or ""),
        (_("Responsable"), (wo.assigned_to.full_name or wo.assigned_to.email) if wo.assigned_to else ""),
        (_("Apoyo"), ", ".join(h.full_name or h.email for h in helpers)),
        (_("Fecha límite"), _d(wo.due_on)),
        (_("Plan"), wo.plan_assignment.plan.name if wo.plan_assignment else ""),
        (_("Inicio"), _dt(wo.started_at)),
        (_("Término"), _dt(wo.completed_at)),
        (_("Cierre"), _dt(wo.closed_at)),
        (_("Creada"), _dt(wo.created_at)),
    ]
    story = [_pairs(data)]
    if wo.description:
        story += [_p(_("Descripción"), H2), _p(wo.description)]
    story += [
        _p(_("Checklist"), H2),
        _grid(
            [_("N.º"), _("Ítem"), _("Resultado"), _("Valor"), _("Nota")],
            [
                (index, item.text, item.get_result_display() or DASH, _checklist_value(item), item.note)
                for index, item in enumerate(checklist, start=1)
            ],
            [0.06, 0.42, 0.13, 0.14, 0.25],
            _("Sin checklist."),
        ),
        _p(_("Costos"), H2),
    ]
    lines = [
        *(
            (_("Mano de obra"), e.user.full_name or e.user.email, f"{e.minutes} min", _money(e.cost))
            for e in wo.labor.all()
        ),
        *(
            (_("Repuesto"), p.description, f"{num(p.quantity)} {p.unit}".strip(), _money(p.cost))
            for p in wo.parts.all()
        ),
        *(
            (
                _("Otro costo"),
                f"{c.description} · {c.supplier}" if c.supplier else c.description,
                "",
                _money(c.cost),
            )
            for c in wo.other_costs.all()
        ),
    ]
    story.append(
        _grid(
            [_("Tipo"), _("Detalle"), _("Cantidad"), _("Costo")],
            lines,
            [0.18, 0.5, 0.14, 0.18],
            _("Sin costos."),
        )
    )
    story.append(
        _pairs(
            [
                (_("Mano de obra"), _money(wo.labor_cost)),
                (_("Repuestos"), _money(wo.parts_cost)),
                (_("Otros costos"), _money(wo.other_cost)),
                (_("Costo total"), _money(wo.total_cost)),
            ]
        )
    )
    if wo.completion_notes:
        story += [_p(_("Comentario de cierre"), H2), _p(wo.completion_notes)]
    image = _signature_image(signature)
    if image is not None:
        story += [_p(_("Firma de conformidad"), H2), Spacer(1, 2 * mm), image]
        if signature.uploaded_by:
            story.append(_p(signature.uploaded_by.full_name or signature.uploaded_by.email, LABEL))
    return _render(
        organization=organization,
        title=_("Orden de trabajo %(code)s") % {"code": wo.code},
        subtitle=wo.title,
        story=story,
    )
