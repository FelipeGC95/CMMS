"""PWA de terreno, P5 (X-06, ADR-011 §1e): salir con borrado, horas de apoyo y endurecimiento.

El comportamiento en el navegador (IndexedDB borrada, confirmación con pendientes, horas que
llegan al servidor) lo prueba tests/e2e/test_field_offline.py; aquí, las piezas del servidor
y los contratos entre plantillas y JS.
"""

import re
from pathlib import Path

from django.conf import settings
from django.urls import reverse

from apps.field import pwa
from tests.conftest import login

STATIC = Path(settings.BASE_DIR) / "static"
WEB_JS = (STATIC / "js" / "app.js").read_text(encoding="utf-8")
FIELD_JS = {path.name: path.read_text(encoding="utf-8") for path in (STATIC / "field").glob("*.js")}


def test_web_js_wipes_the_same_database_the_field_view_uses():
    name = re.search(r'DB_NAME = "([^"]+)"', FIELD_JS["db.js"]).group(1)
    assert f'const FIELD_DB = "{name}";' in WEB_JS
    assert "indexedDB.deleteDatabase(FIELD_DB)" in WEB_JS
    # Abrir la base para contar no la crea si no existe.
    assert "request.transaction.abort()" in WEB_JS
    # Las pestañas de /field/ sueltan la base para no bloquear el borrado.
    assert "onversionchange" in FIELD_JS["db.js"]


def test_web_js_never_parses_html():
    forbidden = re.compile(r"innerHTML|outerHTML|insertAdjacentHTML|document\.write|createContextualFragment")
    assert not forbidden.search(WEB_JS)


def test_no_clear_site_data_header():
    """Decisión de ADR-011: el borrado lo hace la página (IndexedDB), no Clear-Site-Data."""
    sources = [*Path(settings.BASE_DIR, "apps").rglob("*.py"), Path(settings.BASE_DIR, "config")]
    for path in sources:
        if path.is_file():
            assert "Clear-Site-Data" not in path.read_text(encoding="utf-8"), path


def test_exit_dialog_has_its_texts_and_three_choices(client, tenant_a):
    login(client, tenant_a.tech)
    html = client.get(reverse("dashboard")).content.decode()
    dialog = html[html.index('<dialog class="dialog" id="field-exit-dialog"') : html.index("</dialog>")]
    for key in ("exit-one", "exit-other", "switch-one", "switch-other"):
        assert f'data-msg-{key}="' in dialog, key
    assert "{n} cambios de terreno sin enviar" in dialog
    assert 'aria-labelledby="field-exit-title"' in dialog and 'aria-describedby="field-exit-text"' in dialog
    assert f'href="{reverse("field_shell")}" autofocus' in dialog  # "Ir a sincronizar", lo más seguro
    assert 'value="confirm" data-mode="exit"' in dialog and 'value="confirm" data-mode="switch"' in dialog
    assert 'value="cancel"' in dialog
    assert '<form method="dialog"' in dialog


def test_shell_has_an_exit_button_and_loads_the_web_js(client, tenant_a):
    login(client, tenant_a.tech)
    html = client.get(reverse("field_shell")).content.decode()
    assert f'action="{reverse("account_logout")}" data-field-exit' in html
    assert "data-csrf-cookie" in html
    assert 'id="field-exit-dialog"' in html
    assert re.search(r'<script src="[^"]*js/app\.js" defer>', html)
    assert "js/app.js" in pwa.SHELL_STATIC


def test_exit_from_the_saved_shell_logs_out_with_the_cookie_token(tenant_a):
    """El shell guardado no lleva token: app.js envía el valor de la cookie csrftoken."""
    from django.test import Client

    client = Client(enforce_csrf_checks=True)
    login(client, tenant_a.tech)
    client.get(reverse("field_shell"))
    token = client.cookies["csrftoken"].value
    assert client.post(reverse("account_logout")).status_code == 403  # sin token no sale
    response = client.post(reverse("account_logout"), {"csrfmiddlewaretoken": token})
    assert response.status_code == 302
    assert client.get(reverse("field_shell")).status_code == 302  # ya no hay sesión


def test_queued_changes_carry_their_organization_to_the_server():
    assert 'headers["X-Field-Organization"]' in FIELD_JS["api.js"]
    assert "const org = entry.organization_id;" in FIELD_JS["sync.js"]
    assert FIELD_JS["sync.js"].count(", org)") + FIELD_JS["sync.js"].count("org,\n") >= 9
    assert '"organization_changed") return "identity"' in FIELD_JS["api.js"]


def test_shell_has_the_labor_form(client, tenant_a):
    login(client, tenant_a.tech)
    html = client.get(reverse("field_shell")).content.decode()
    assert 'data-slot="labor"' in html and 'data-form="labor"' in html
    assert 'name="minutes"' in html and 'max="1440"' in html
    for key in ("kind-labor", "saved-labor", "invalid-minutes"):
        assert f'data-msg-{key}="' in html, key


def test_labor_is_queued_and_sent_to_the_labor_endpoint():
    assert 'kind: "labor"' in FIELD_JS["app.js"]
    assert 'case "labor":' in FIELD_JS["sync.js"]
    assert "labor/`, { ...payload, client_uuid: entry.client_uuid }" in FIELD_JS["sync.js"]
    pk = "01900000-0000-7000-8000-000000000000"
    assert reverse("field-work-order-labor", args=[pk]) == f"/field/api/work-orders/{pk}/labor/"


def test_field_view_waits_for_bootstrap_before_showing_saved_data_online():
    app = FIELD_JS["app.js"]
    assert "app.refreshing && !app.verified" in app
    assert "if (navigator.onLine) refresh();" in app


def test_service_worker_does_not_cache_a_redirect_as_the_shell(client, db):
    body = client.get(reverse("field_sw")).content.decode()
    assert 'redirect: "error"' in body
    assert "!response.redirected" in body


def test_design_system_shows_the_exit_dialog_and_labor_form(client, tenant_a):
    login(client, tenant_a.admin)
    html = client.get(reverse("design_system")).content.decode()
    for css_class in ("dialog__actions", "field-bar__exit", "field-labor"):
        assert css_class in html, css_class
