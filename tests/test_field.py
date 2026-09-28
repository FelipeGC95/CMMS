"""Vista de terreno sin conexión (X-06, ADR-011): shell, Service Worker, manifest y módulos ES."""

import datetime
import json
import re
import shutil
from pathlib import Path

import pytest
from django.conf import settings
from django.contrib.staticfiles import finders
from django.core.management import call_command
from django.urls import reverse
from PIL import Image

from apps.field import pwa
from apps.field.views import DEMO_FIXTURE, demo_payload
from apps.ui.tokens import color_tokens
from tests.conftest import login
from tests.test_csp import EVENT_HANDLER, INLINE_SCRIPT, INLINE_STYLE

STATIC = Path(settings.BASE_DIR) / "static"
FIELD_STATIC = STATIC / "field"


# ---------- Service Worker ----------


def test_service_worker_is_served_without_session(client, db):
    response = client.get(reverse("field_sw"))
    assert response.status_code == 200
    assert response["Content-Type"].startswith("application/javascript")
    assert response["Cache-Control"] == "no-cache"
    body = response.content.decode()
    assert f'const CACHE_VERSION = "{pwa.cache_version()}";' in body
    for url in pwa.precache_urls():
        assert json.dumps(url) in body
    assert "/field/api/" not in json.dumps(pwa.precache_urls())


def test_service_worker_keeps_no_cache_with_session(client, tenant_a):
    login(client, tenant_a.tech)
    response = client.get(reverse("field_sw"))
    assert response.status_code == 200
    assert response["Cache-Control"] == "no-cache"
    assert tenant_a.org.name not in response.content.decode()


def test_service_worker_version_changes_when_a_static_file_changes(tmp_path, settings, db):
    copy = tmp_path / "static"
    shutil.copytree(STATIC, copy)
    settings.STATICFILES_DIRS = [copy]
    before = pwa.cache_version()
    assert pwa.cache_version() == before  # estable mientras nada cambie
    (copy / "field" / "ui.js").write_text("// cambio\n", encoding="utf-8")
    assert pwa.cache_version() != before


def test_service_worker_version_changes_with_the_release(settings, db):
    settings.APP_RELEASE = "abc123"
    first = pwa.cache_version()
    settings.APP_RELEASE = "def456"
    assert pwa.cache_version() != first


def test_precache_covers_the_shell_and_its_static_files():
    urls = pwa.precache_urls()
    assert urls[0] == reverse("field_shell")
    assert reverse("field_manifest") in urls
    for path in pwa.SHELL_STATIC:
        assert finders.find(path), f"falta el estático {path}"


# ---------- Manifest ----------


def test_manifest_is_valid_and_its_icons_exist(client, db):
    response = client.get(reverse("field_manifest"))
    assert response.status_code == 200
    assert response["Content-Type"] == "application/manifest+json"
    data = json.loads(response.content)
    assert data["name"] == "GALBUST · Terreno"
    assert data["start_url"] == data["scope"] == reverse("field_shell")
    assert data["display"] == "standalone" and data["lang"] == "es-CL"
    purposes = {(icon["sizes"], icon["purpose"]) for icon in data["icons"]}
    assert {("192x192", "any"), ("512x512", "any"), ("512x512", "maskable")} <= purposes
    for path, sizes, _purpose in pwa.ICONS:
        found = finders.find(path)
        assert found, path
        width, height = (int(n) for n in sizes.split("x"))
        with Image.open(found) as image:
            assert image.size == (width, height) and image.format == "PNG"


def test_maskable_icon_fills_the_whole_square():
    """Maskable: fondo a sangre (sin transparencia) para que el sistema pueda recortarlo."""
    with Image.open(finders.find("brand/galbust-app-icon-maskable-512.png")) as image:
        rgba = image.convert("RGBA")
        assert rgba.getpixel((0, 0))[3] == 255 and rgba.getpixel((511, 511))[3] == 255
        # El isotipo queda dentro de la zona segura (círculo de 80 %): el borde es solo fondo.
        corner = rgba.getpixel((0, 0))
        ring = [rgba.getpixel((x, 51)) for x in range(0, 512, 16)]
        assert all(pixel == corner for pixel in ring)


def test_manifest_colors_match_design_tokens():
    light = color_tokens()["light"]
    assert pwa.THEME_COLOR.upper() == light["--color-nav-bg"].upper()
    assert pwa.BACKGROUND_COLOR.upper() == light["--color-bg"].upper()


# ---------- Shell ----------


def test_shell_requires_login(client, db):
    response = client.get(reverse("field_shell"))
    assert response.status_code == 302
    assert reverse("account_login") in response["Location"]


def test_shell_has_no_organization_or_personal_data(client, tenant_a):
    login(client, tenant_a.tech)
    response = client.get(reverse("field_shell"))
    assert response.status_code == 200
    body = response.content.decode()
    user = tenant_a.tech.user
    for value in (tenant_a.org.name, tenant_a.org.slug, user.full_name, user.email, tenant_a.asset.code):
        assert value not in body, value
    assert str(tenant_a.org.pk) not in body and str(user.pk) not in body
    assert "csrftoken" in response.cookies


def test_shell_is_the_same_for_every_organization(client, tenant_a, tenant_b):
    login(client, tenant_a.tech)
    first = client.get(reverse("field_shell")).content.decode()
    client.logout()
    login(client, tenant_b.admin)
    second = client.get(reverse("field_shell")).content.decode()
    assert first == second


def test_shell_sends_strict_csp_and_has_no_inline_code(client, tenant_a):
    login(client, tenant_a.tech)
    response = client.get(reverse("field_shell"))
    policy = response["Content-Security-Policy"]
    assert "script-src 'self'" in policy and "style-src 'self'" in policy and "unsafe" not in policy
    html = response.content.decode()
    assert not INLINE_SCRIPT.search(html)
    assert not INLINE_STYLE.search(html)
    assert not EVENT_HANDLER.search(html)
    assert re.search(r'<script type="module" src="[^"]*field/app\.js">', html)
    assert f'href="{reverse("field_manifest")}"' in html


def test_shell_passes_texts_to_js_through_data_attributes(client, tenant_a):
    login(client, tenant_a.tech)
    html = client.get(reverse("field_shell")).content.decode()
    assert "Sin conexión · sin datos descargados" in html
    assert 'data-msg-offline="Sin conexión"' in html
    assert 'id="tpl-wo-item"' in html and 'id="tpl-detail"' in html


def test_field_js_never_parses_html():
    """Solo textContent y nodos: nada de innerHTML y parecidos (los datos podrían traer marcas)."""
    forbidden = re.compile(r"innerHTML|outerHTML|insertAdjacentHTML|document\.write|createContextualFragment")
    files = sorted(FIELD_STATIC.rglob("*.js"))
    assert files
    offenders = [path.name for path in files if forbidden.search(path.read_text(encoding="utf-8"))]
    assert offenders == []


# ---------- Datos de demostración (contrato de §2 para P2) ----------

BOOTSTRAP_KEYS = {
    "user",
    "organization",
    "role",
    "permissions",
    "horizon_days",
    "status_labels",
    "execution_transitions",
    "server_time",
    "min_client_version",
}
WORK_ORDER_KEYS = {
    "id",
    "code",
    "title",
    "work_type",
    "status",
    "priority",
    "scheduled_for",
    "due_on",
    "estimated_minutes",
    "description",
    "assigned_to_me",
    "assigned_to",
    "asset",
    "location",
    "meter",
    "meter_required",
    "allowed_transitions",
    "updated_at",
    "checklist",
}
ITEM_KEYS = {
    "id",
    "position",
    "section",
    "text",
    "item_type",
    "unit",
    "min_value",
    "max_value",
    "required",
    "result",
    "value_number",
    "value_text",
    "note",
    "answered_at",
    "photo_count",
}


def test_demo_fixture_follows_the_field_api_contract():
    data = json.loads(DEMO_FIXTURE.read_text(encoding="utf-8"))
    assert set(data) == {"bootstrap", "snapshot"}
    assert set(data["bootstrap"]) == BOOTSTRAP_KEYS
    assert set(data["bootstrap"]["organization"]) == {"id", "name", "timezone", "require_failure_details"}
    assert set(data["snapshot"]) == {"generated_at", "days", "truncated", "work_orders"}
    for wo in data["snapshot"]["work_orders"]:
        assert set(wo) == WORK_ORDER_KEYS, wo["code"]
        for item in wo["checklist"]:
            assert set(item) == ITEM_KEYS
    # Asignadas a la persona primero (decisión de producto).
    mine = [wo["assigned_to_me"] for wo in data["snapshot"]["work_orders"]]
    assert any(mine) and not all(mine)


def test_demo_payload_moves_dates_to_today():
    today = datetime.date(2027, 1, 10)
    data = demo_payload(today)
    assert data["bootstrap"]["server_time"].startswith("2027-01-10")
    scheduled = {wo["scheduled_for"] for wo in data["snapshot"]["work_orders"] if wo["scheduled_for"]}
    assert "2027-01-10" in scheduled


def test_demo_data_is_only_served_in_debug(client, tenant_a, settings):
    login(client, tenant_a.tech)
    settings.DEBUG = False
    assert client.get(reverse("field_demo")).status_code == 404
    settings.DEBUG = True
    response = client.get(reverse("field_demo"))
    assert response.status_code == 200
    assert set(response.json()) == {"bootstrap", "snapshot"}


# ---------- Módulos ES con hash (config/storage.py) ----------


def test_collectstatic_rewrites_es_module_imports_to_hashed_urls(tmp_path, settings):
    settings.STATIC_ROOT = tmp_path / "staticfiles"
    settings.STORAGES = {
        **settings.STORAGES,
        "staticfiles": {"BACKEND": "config.storage.ModuleManifestStaticFilesStorage"},
    }
    call_command("collectstatic", interactive=False, verbosity=0)

    manifest = json.loads((tmp_path / "staticfiles" / "staticfiles.json").read_text(encoding="utf-8"))[
        "paths"
    ]
    app = (tmp_path / "staticfiles" / manifest["field/app.js"]).read_text(encoding="utf-8")
    for module in ("db", "ui"):
        hashed = Path(manifest[f"field/{module}.js"]).name
        assert re.fullmatch(rf"{module}\.[0-9a-f]{{12}}\.js", hashed)
        assert f'from "./{hashed}"' in app
    assert 'from "./db.js"' not in app


def test_production_storage_rewrites_module_imports():
    from config.storage import ModuleManifestStaticFilesStorage

    assert ModuleManifestStaticFilesStorage.support_js_module_import_aggregation is True
    source = (Path(settings.BASE_DIR) / "config" / "settings.py").read_text(encoding="utf-8")
    assert '"config.storage.ModuleManifestStaticFilesStorage"' in source


@pytest.mark.parametrize("path", sorted(p.name for p in FIELD_STATIC.glob("*.js")))
def test_field_modules_use_relative_imports(path):
    """Los `import` relativos son los que la reescritura con hash sabe resolver."""
    source = (FIELD_STATIC / path).read_text(encoding="utf-8")
    for target in re.findall(r'from\s+"([^"]+)"', source):
        assert target.startswith("./") and (FIELD_STATIC / target[2:]).exists(), target
