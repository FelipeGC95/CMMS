"""Importación de lecturas por CSV (AST-09): vista previa con errores por fila, confirmación, idempotencia."""

import io

import pytest
from django.urls import reverse

from apps.assets.models import Meter, MeterKind, MeterReading
from apps.assets.reading_imports import ReadingImportFileError, build_reading_preview, parse_csv
from apps.core.tenancy import tenant_context
from tests.conftest import login

pytestmark = pytest.mark.django_db


def _csv(text):
    file = io.BytesIO(text.encode("utf-8-sig"))
    file.name = "lecturas.csv"
    file.size = len(file.getvalue())
    return file


@pytest.fixture
def meter(tenant_a):
    with tenant_context(tenant_a.org):
        return Meter.objects.create(asset=tenant_a.asset, kind=MeterKind.HOURS)


def test_parse_accepts_semicolon_and_spanish_headers():
    rows = parse_csv(_csv("Activo;Valor;Fecha;Reinicio\nCMP-01;1.250,5;24-09-2026 08:00;sí\n"))
    assert rows == [
        {
            "asset_code": "CMP-01",
            "value": "1.250,5",
            "read_at": "24-09-2026 08:00",
            "is_reset": "sí",
            "_row": 2,
        }
    ]


def test_parse_rejects_missing_columns():
    with pytest.raises(ReadingImportFileError):
        parse_csv(_csv("activo,valor\nCMP-01,5\n"))


def test_preview_reports_errors_per_row_without_saving(in_a, meter):
    raw = parse_csv(
        _csv(
            "asset_code,value,read_at,is_reset\n"
            "CMP-01,100,2026-09-20 08:00,\n"
            "CMP-01,90,2026-09-21 08:00,\n"  # retrocede sin reinicio
            "CMP-01,5,2026-09-22 08:00,si\n"  # reinicio: vale
            "NOPE,5,2026-09-22,\n"
            "CMP-01,abc,2026-09-22,\n"
            "CMP-01,5,ayer,\n"
            "CMP-01,5,2999-01-01,\n"
            "CMP-01,100,2026-09-20 08:00,\n"  # repetida en el archivo
        )
    )
    preview = build_reading_preview(raw, membership=in_a.admin)
    assert [r.action for r in preview.rows] == [
        "create",
        "error",
        "create",
        "error",
        "error",
        "error",
        "error",
        "duplicate",
    ]
    assert "menor que la anterior" in preview.rows[1].errors[0]
    assert MeterReading.objects.count() == 0


def test_asset_with_several_meters_needs_meter_id(in_a, meter):
    other = Meter.objects.create(asset=in_a.asset, kind=MeterKind.KM)
    raw = [{"asset_code": "CMP-01", "value": "1", "read_at": "2026-09-20", "_row": 2}]
    assert "varios medidores" in build_reading_preview(raw, membership=in_a.admin).rows[0].errors[0]
    raw[0]["meter"] = str(other.pk)
    assert build_reading_preview(raw, membership=in_a.admin).rows[0].action == "create"


def test_web_upload_preview_confirm_and_reimport_is_idempotent(client, tenant_a, meter):
    login(client, tenant_a.admin)
    text = (
        "asset_code,value,read_at\nCMP-01,10,2026-09-20 08:00\nCMP-01,20,2026-09-21 08:00\nX,1,2026-09-21\n"
    )
    response = client.post(reverse("reading_import"), {"file": _csv(text)})
    assert response.context["counts"] == {"create": 2, "duplicate": 0, "error": 1}
    with tenant_context(tenant_a.org):
        assert MeterReading.objects.count() == 0
    token = response.context["token"]
    client.post(reverse("reading_import_confirm", args=[token]))
    with tenant_context(tenant_a.org):
        readings = list(MeterReading.objects.order_by("read_at"))
        meter.refresh_from_db()
    assert [r.source for r in readings] == ["import", "import"]
    assert meter.current_value == 20
    again = client.post(reverse("reading_import"), {"file": _csv(text)})
    assert again.context["counts"] == {"create": 0, "duplicate": 2, "error": 1}


def test_expired_preview_token_redirects(client, tenant_a):
    login(client, tenant_a.admin)
    response = client.post(reverse("reading_import_confirm", args=["nope"]))
    assert response.status_code == 302 and response["Location"] == reverse("reading_import")
