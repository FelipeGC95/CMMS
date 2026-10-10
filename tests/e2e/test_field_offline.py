"""E2E de la PWA de terreno sin conexión (X-06, ADR-011) con Playwright.

No corre en el CI normal: necesita un servidor con `seed_demo` y Playwright, que no es
dependencia del proyecto. Para correrlo:

    uv run python manage.py migrate
    uv run python manage.py seed_demo --reset
    uv run python manage.py runserver 127.0.0.1:8765
    # en otra terminal (E2E_BASE_URL activa la prueba):
    $env:E2E_BASE_URL = "http://127.0.0.1:8765"
    uv run --no-project --with playwright --with pytest pytest -c tests/e2e/pytest.ini tests/e2e

El estado en la base de datos se revisa con `manage.py shell` del proyecto (la misma base
que usa el servidor).
"""

import base64
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

BASE_URL = os.environ.get("E2E_BASE_URL", "")
pytestmark = pytest.mark.skipif(not BASE_URL, reason="E2E_BASE_URL no definida (e2e solo a pedido)")
playwright = pytest.importorskip("playwright.sync_api") if BASE_URL else None

REPO = Path(__file__).resolve().parents[2]
TECH = ("tecnico@demo.cl", os.environ.get("DEMO_PASSWORD") or "demo-galbust-2026")
# PNG de 8×8 px: basta para probar la foto (el servidor la convierte a JPEG).
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAgAAAAICAIAAABLbSncAAAAFElEQVR4nGM8oaHBgA0wYRUdtBIA4DgBKJ8lCQoAAAAASUVORK5CYII="
)


def manage(code: str):
    """Corre Python en `manage.py shell` del proyecto y devuelve el JSON que imprime."""
    env = {k: v for k, v in os.environ.items() if k != "VIRTUAL_ENV"}
    command = ["uv", "run", "python", "manage.py", "shell", "-c", code]
    result = subprocess.run(command, cwd=REPO, env=env, capture_output=True, text=True, encoding="utf-8")
    if result.returncode:
        sys.stderr.write(result.stderr)
        raise AssertionError("manage.py shell falló")
    return json.loads(result.stdout.strip().splitlines()[-1])


PICK = """
import json
from apps.accounts.models import User
from apps.work.models import WorkOrder, WorkOrderChecklistItem as Item
tech = User.objects.get(email="tecnico@demo.cl")
shown = {shown!r}
qs = (
    WorkOrder.all_tenants.filter(assigned_to=tech, status__in=["planned", "scheduled"])
    .exclude(work_type="corrective")
    .filter(pk__in=shown)
    .order_by("due_on", "created_at")
)
for wo in qs:
    items = list(Item.all_tenants.filter(work_order=wo).order_by("order"))
    if {need_items} and not items:
        continue
    print(json.dumps({{"id": str(wo.pk), "code": wo.code, "items": [
        {{"id": str(i.pk), "type": i.item_type, "min": None if i.min_value is None else float(i.min_value),
          "max": None if i.max_value is None else float(i.max_value)}} for i in items]}}))
    break
else:
    print("null")
"""

STATE = """
import json
from apps.attachments.models import Attachment
from apps.work.models import LaborEntry, WorkOrder, WorkOrderChecklistItem as Item
wo = WorkOrder.all_tenants.get(pk="{id}")
items = list(Item.all_tenants.filter(work_order=wo))
targets = [i.pk for i in items] + [wo.pk]
photos = Attachment.all_tenants.filter(target_id__in=targets, purpose="photo").count()
labor = list(LaborEntry.all_tenants.filter(work_order=wo).values_list("minutes", flat=True))
answered = sum(1 for i in items if i.result)
state = {{"status": wo.status, "labor": labor, "answered": answered, "items": len(items)}}
print(json.dumps({{**state, "photos": photos}}))
"""


@pytest.fixture
def browser():
    with playwright.sync_playwright() as p:
        instance = p.chromium.launch()
        yield instance
        instance.close()


def field_page(browser):
    context = browser.new_context(viewport={"width": 360, "height": 800}, base_url=BASE_URL)
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto("/accounts/login/")
    page.fill("input[name=login]", TECH[0])
    page.fill("input[name=password]", TECH[1])
    page.click("button[type=submit]")
    page.wait_for_load_state("networkidle")
    page.goto("/field/")
    page.wait_for_selector(".field-days, [data-slot='none']:not([hidden])", timeout=20000)
    return context, page, errors


def pick(page, *, need_items):
    """Una OT planificada del técnico entre las que la vista descargó."""
    shown = page.eval_on_selector_all(
        ".field-wo__link", "links => links.map((a) => decodeURIComponent(a.hash.replace('#/wo/', '')))"
    )
    return manage(PICK.format(shown=shown, need_items=need_items))


def item(page, item_id):
    return page.locator(f"#item-{item_id}")


def answer_item(page, row, photo_file):
    li = item(page, row["id"])
    if row["type"] == "measurement":
        low, high = row["min"], row["max"]
        value = (low + high) / 2 if low is not None and high is not None else (low or high or 1)
        li.locator("[data-slot='value']").fill(str(value).replace(".", ","))
        li.locator("[data-action='answer']").click()
    elif row["type"] == "text":
        li.locator("[data-slot='value-text']").fill("Sin novedad")
        li.locator("[data-action='answer']").click()
    elif row["type"] == "signature":
        canvas = li.locator("canvas")
        canvas.scroll_into_view_if_needed()
        box = canvas.bounding_box()
        page.mouse.move(box["x"] + 20, box["y"] + 40)
        page.mouse.down()
        page.mouse.move(box["x"] + 120, box["y"] + 90, steps=8)
        page.mouse.up()
        li.locator("[data-action='answer']").click()
    elif row["type"] == "photo":
        li.locator("input[type=file]").set_input_files(photo_file)
    else:
        li.locator("[data-result='ok']").click()
    saved = item(page, row["id"]).locator("[data-slot='saved']:not(:empty)")
    saved.wait_for()
    assert "field-saved--error" not in (saved.get_attribute("class") or ""), saved.inner_text()


def lose_first_response(page, pattern, seen):
    """El servidor procesa el primer envío pero el teléfono no recibe la respuesta: el
    reintento con el mismo client_uuid no debe duplicar nada."""

    def handler(route):
        if route.request.method == "POST" and pattern not in seen:
            seen.add(pattern)
            route.fetch()
            route.abort("connectionreset")
        else:
            route.continue_()

    page.route(f"**{pattern}", handler)


def test_offline_execution_reaches_the_server_once(browser, tmp_path):
    photo_file = tmp_path / "foto.png"
    photo_file.write_bytes(PNG)
    context, page, errors = field_page(browser)
    wo = pick(page, need_items=True)
    assert wo, "seed_demo no tiene una OT planificada con checklist para el técnico"

    context.set_offline(True)
    page.evaluate("id => { location.hash = `#/wo/${id}`; }", wo["id"])
    page.wait_for_selector(".field-detail")
    start = page.locator("[data-transition='start']")
    if start.is_visible():
        start.click()
        page.wait_for_selector("[data-transition='pause']:not([hidden])")
    for row in wo["items"]:
        answer_item(page, row, photo_file)
    # Una foto extra en el primer ítem (además de las de ítems «foto»).
    first = wo["items"][0]["id"]
    item(page, first).locator("input[type=file]").set_input_files(photo_file)
    item(page, first).locator("[data-slot='saved']:not(:empty)").wait_for()
    assert "Sin conexión" in page.inner_text("#field-sync-rows")

    page.click("[data-slot='complete']")
    page.wait_for_selector("form[data-form='complete']")
    page.fill("#fc-labor", "30")
    if page.locator("#fc-meter").is_visible():
        page.fill("#fc-meter", "999999")
    page.click("form[data-form='complete'] button[type=submit]")
    if os.environ.get("E2E_SHOTS"):
        page.screenshot(path=f"{os.environ['E2E_SHOTS']}/complete-submitted.png", full_page=True)
    page.wait_for_selector(".field-detail [data-slot='flash']:not([hidden])")

    seen = set()
    lose_first_response(page, "/photos/", seen)
    lose_first_response(page, "/complete/", seen)
    context.set_offline(False)
    page.evaluate("window.dispatchEvent(new Event('online'))")
    page.wait_for_selector(".field-sync__row--success", timeout=60000)
    assert seen == {"/photos/", "/complete/"}

    state = manage(STATE.format(id=wo["id"]))
    photo_items = sum(1 for row in wo["items"] if row["type"] == "photo")
    assert state["status"] in ("completed", "closed")  # sin revisión, el cierre es automático
    assert state["labor"] == [30]
    assert state["answered"] == state["items"]
    assert state["photos"] == photo_items + 1
    assert errors == []
    context.close()


def test_offline_request_with_photo_and_reading_are_sent(browser, tmp_path):
    photo_file = tmp_path / "foto.png"
    photo_file.write_bytes(PNG)
    context, page, errors = field_page(browser)
    context.set_offline(True)
    description = f"Fuga de aceite e2e {os.getpid()}"
    page.evaluate("() => { location.hash = '#/request'; }")
    page.wait_for_selector("form[data-form='request']")
    page.select_option("#fr-asset", index=1)
    page.fill("#fr-description", description)
    page.set_input_files("#fr-photo", photo_file)
    page.wait_for_selector("[data-slot='photo-status']:not(:empty)")
    page.click("form[data-form='request'] button[type=submit]")
    page.wait_for_selector(".field-days")

    page.evaluate("() => { location.hash = '#/reading'; }")
    page.wait_for_selector("form[data-form='reading']")
    page.select_option("#fm-meter", index=1)
    page.fill("#fm-value", "999999")
    page.click("form[data-form='reading'] button[type=submit]")
    page.wait_for_selector(".field-days")
    assert "Sin conexión" in page.inner_text("#field-sync-rows")

    context.set_offline(False)
    page.wait_for_selector(".field-sync__row--success", timeout=60000)
    page.wait_for_selector("[data-slot='recent']:not([hidden])", timeout=10000)
    state = manage(
        "import json\nfrom apps.attachments.models import Attachment\n"
        "from apps.work.models import WorkRequest\n"
        f"r = WorkRequest.all_tenants.filter(description={description!r})\n"
        "wo = r.first().work_order_id if r.exists() else None\n"
        "photos = Attachment.all_tenants.filter(target_id=wo, purpose='photo').count() if wo else 0\n"
        "print(json.dumps({'requests': r.count(), 'photos': photos}))"
    )
    assert state["requests"] == 1
    assert state["photos"] == 1
    assert errors == []
    context.close()


def test_cancelled_work_order_is_a_conflict_that_can_be_discarded(browser):
    context, page, errors = field_page(browser)
    wo = pick(page, need_items=False)
    assert wo, "seed_demo no tiene otra OT planificada para el técnico"
    page.on("dialog", lambda dialog: dialog.accept())
    context.set_offline(True)
    page.evaluate("id => { location.hash = `#/wo/${id}`; }", wo["id"])
    page.click("[data-transition='start']")
    page.wait_for_selector("[data-transition='pause']:not([hidden])")

    # Mientras tanto, en la web, alguien cancela la OT.
    manage(
        "import json\nfrom apps.work.models import WorkOrder\n"
        f"WorkOrder.all_tenants.filter(pk='{wo['id']}').update(status='cancelled')\nprint('1')"
    )
    context.set_offline(False)
    page.evaluate("window.dispatchEvent(new Event('online'))")
    page.wait_for_selector(".field-sync__row--error", timeout=60000)

    page.evaluate("() => { location.hash = '#/conflicts'; }")
    card = page.locator(".field-problem")
    card.wait_for()
    assert "cancelada" in card.inner_text().lower()
    card.locator("[data-action='discard']").click()
    page.wait_for_selector("[data-slot='none']:not([hidden])")
    assert manage(STATE.format(id=wo["id"]))["status"] == "cancelled"
    assert errors == []
    context.close()
