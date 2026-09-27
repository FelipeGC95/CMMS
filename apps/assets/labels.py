"""Etiquetas QR en PDF, varias por hoja A4 (AST-05 CA2, ADR-019).

Se dibujan con reportlab (Python puro, sin librerías del sistema) y el QR con
segno, el mismo que usa la ficha del activo. Cada etiqueta lleva el QR del token
no adivinable, el código, el nombre y el logo de la organización; sin logo, el
nombre de la organización.
"""

import io
from dataclasses import dataclass

import segno
from django.utils.translation import gettext_lazy as _
from reportlab.lib.colors import HexColor
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib.utils import simpleSplit
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen import canvas

from apps.core import pdf as pdf_fonts

# Tope de etiquetas por PDF: 500 son 21 hojas de 24; más que eso se filtra la lista.
MAX_LABELS = 500

# Colores de impresión (un PDF no usa los tokens CSS): tinta de la marca sobre blanco.
INK = HexColor("#0B2E4F")
CUT_LINE = HexColor("#B8C2CC")

PAGE_MARGIN = 6 * mm  # dentro del área que imprime cualquier impresora de oficina
CELL_PADDING = 3 * mm
QR_BORDER_MODULES = 2  # zona silenciosa del QR; el relleno de la celda suma el resto

# Fuentes TTF incrustadas (DejaVu, ver apps/core/pdf.py): cubren «Ñandú – Nº 3 ø25».
TEXT_FONT = pdf_fonts.TEXT_FONT
ORG_FONT = pdf_fonts.BOLD_FONT
CODE_FONT = pdf_fonts.MONO_BOLD_FONT
register_fonts = pdf_fonts.register_fonts


@dataclass(frozen=True)
class LabelLayout:
    columns: int
    rows: int
    label: str
    org_size: float  # puntos
    code_size: float
    name_size: float

    @property
    def per_page(self) -> int:
        return self.columns * self.rows


LAYOUTS = {
    "3x8": LabelLayout(3, 8, _("Pequeñas · 24 por hoja"), 7, 11, 8),
    "2x5": LabelLayout(2, 5, _("Grandes · 10 por hoja"), 10, 16, 11),
}
DEFAULT_LAYOUT = "3x8"


def _qr_rows(url: str):
    """Filas del QR como listas de tramos oscuros (inicio, largo), en módulos."""
    rows = []
    for row in segno.make(url, error="m").matrix_iter(border=QR_BORDER_MODULES):
        runs, start = [], None
        for index, dark in enumerate(row):
            if dark and start is None:
                start = index
            elif not dark and start is not None:
                runs.append((start, index - start))
                start = None
        if start is not None:
            runs.append((start, len(row) - start))
        rows.append(runs)
    return rows


def _draw_qr(pdf, url, x, y, size):
    rows = _qr_rows(url)
    module = size / len(rows)
    path = pdf.beginPath()
    for row_index, runs in enumerate(rows):
        top = y + size - (row_index + 1) * module
        for start, length in runs:
            path.rect(x + start * module, top, length * module, module)
    pdf.setFillColor(INK)
    pdf.drawPath(path, stroke=0, fill=1)


def _fit_line(text, font, size, width, min_size):
    """Achica la fuente hasta que el texto quepa; si no alcanza, lo corta con «…»."""
    while size > min_size and stringWidth(text, font, size) > width:
        size -= 0.5
    if stringWidth(text, font, size) > width:
        while text and stringWidth(text + "…", font, size) > width:
            text = text[:-1]
        text += "…"
    return text, size


def _wrap(text, font, size, width, max_lines):
    """Parte el texto en líneas; corta con «…» lo que no cabe (palabras largas o líneas de más)."""
    lines = simpleSplit(text, font, size, width) or [""]
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] += "…"
    return [_fit_line(line, font, size, width, size)[0] for line in lines]


_read_logo = pdf_fonts.read_logo


def _draw_label(pdf, *, asset, url, organization, logo, layout, x, y, width, height):
    inner_x, inner_y = x + CELL_PADDING, y + CELL_PADDING
    inner_w, inner_h = width - 2 * CELL_PADDING, height - 2 * CELL_PADDING
    qr_size = min(inner_h, inner_w * 0.5)
    _draw_qr(pdf, url, inner_x, inner_y + (inner_h - qr_size) / 2, qr_size)

    text_x = inner_x + qr_size + 2 * mm
    text_w = inner_x + inner_w - text_x
    top = inner_y + inner_h
    pdf.setFillColor(INK)

    # Cabecera: logo (alto máximo ~25 % de la etiqueta) o nombre de la organización.
    header_h = inner_h * 0.26
    if logo is not None:
        pdf.drawImage(
            logo,
            text_x,
            top - header_h,
            width=text_w,
            height=header_h,
            preserveAspectRatio=True,
            anchor="nw",
            mask="auto",
        )
        cursor = top - header_h - 1.5 * mm
    else:
        org_name, org_size = _fit_line(organization.name, ORG_FONT, layout.org_size, text_w, 5)
        pdf.setFont(ORG_FONT, org_size)
        pdf.drawString(text_x, top - org_size, org_name)
        cursor = top - org_size - 1.5 * mm

    code, code_size = _fit_line(asset.code, CODE_FONT, layout.code_size, text_w, 6)
    pdf.setFont(CODE_FONT, code_size)
    cursor -= code_size * 0.8
    pdf.drawString(text_x, cursor, code)
    cursor -= 1.2 * mm

    leading = layout.name_size * 1.15
    max_lines = max(1, int((cursor - inner_y) // leading))
    pdf.setFont(TEXT_FONT, layout.name_size)
    for line in _wrap(asset.name, TEXT_FONT, layout.name_size, text_w, max_lines):
        cursor -= leading
        pdf.drawString(text_x, cursor, line)


def _draw_cut_lines(pdf, layout, cell_w, cell_h):
    """Líneas punteadas de corte entre etiquetas y en el borde de la grilla."""
    pdf.setStrokeColor(CUT_LINE)
    pdf.setLineWidth(0.4)
    pdf.setDash(2, 2)
    left, bottom = PAGE_MARGIN, PAGE_MARGIN
    right, top = left + layout.columns * cell_w, bottom + layout.rows * cell_h
    for column in range(layout.columns + 1):
        pdf.line(left + column * cell_w, bottom, left + column * cell_w, top)
    for row in range(layout.rows + 1):
        pdf.line(left, bottom + row * cell_h, right, bottom + row * cell_h)
    pdf.setDash()


def render_labels_pdf(*, assets, organization, qr_url, layout_key: str = DEFAULT_LAYOUT) -> bytes:
    """PDF A4 con una etiqueta por activo, en el orden recibido.

    `qr_url(token)` devuelve la URL absoluta del QR del activo (`asset_qr`).
    """
    register_fonts()
    layout = LAYOUTS.get(layout_key, LAYOUTS[DEFAULT_LAYOUT])
    page_w, page_h = A4
    cell_w = (page_w - 2 * PAGE_MARGIN) / layout.columns
    cell_h = (page_h - 2 * PAGE_MARGIN) / layout.rows
    logo = _read_logo(organization)

    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4, pageCompression=1)
    pdf.setTitle(str(_("Etiquetas QR · %(name)s") % {"name": organization.name}))
    pdf.setCreator("GALBUST CMMS")
    assets = list(assets)
    for index, asset in enumerate(assets):
        slot = index % layout.per_page
        if slot == 0:
            if index:
                pdf.showPage()
            _draw_cut_lines(pdf, layout, cell_w, cell_h)
        column, row = slot % layout.columns, slot // layout.columns
        _draw_label(
            pdf,
            asset=asset,
            url=qr_url(asset.qr_token),
            organization=organization,
            logo=logo,
            layout=layout,
            x=PAGE_MARGIN + column * cell_w,
            y=page_h - PAGE_MARGIN - (row + 1) * cell_h,
            width=cell_w,
            height=cell_h,
        )
    pdf.showPage()
    pdf.save()
    return buffer.getvalue()
