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
    # Horas de un apoyo (P5): se encolan aparte del cierre.
    page.fill("#fl-minutes", "15")
    page.click("form[data-form='labor'] button[type=submit]")
    page.wait_for_selector("[data-slot='labor-saved']:not(:empty)")

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
    assert sorted(state["labor"]) == [15, 30]
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


# ---------- F1: hallazgo sin conexión (WO-06 CA2) ----------

FINDING_STATE = """
import json
from apps.work.models import WorkOrder, WorkRequest
wo = WorkOrder.all_tenants.get(pk="{id}")
children = list(WorkOrder.all_tenants.filter(source_checklist_item_id="{item}"))
requests = WorkRequest.all_tenants.filter(work_order__in=children)
print(json.dumps({{"status": wo.status, "correctives": len(children),
    "parent": [str(c.parent_work_order_id) for c in children], "requests": requests.count(),
    "stopped": [r.asset_stopped for r in requests], "description": [c.description for c in children]}}))
"""


CORRECTIVE_CODE = """
import json
from apps.work.models import WorkOrder
print(json.dumps(WorkOrder.all_tenants.get(source_checklist_item_id="{item}").code))
"""


def finding_shot(page, name, locator):
    """Capturas del hallazgo (E2E_SHOTS) a 360, 1024 y 1440 px, sin scroll horizontal."""
    if not os.environ.get("E2E_SHOTS"):
        return
    for width in (360, 1024, 1440):
        page.set_viewport_size({"width": width, "height": 900})
        locator.scroll_into_view_if_needed()
        locator.screenshot(path=f"{os.environ['E2E_SHOTS']}/f1-{name}-{width}.png")
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), width
    page.set_viewport_size({"width": 360, "height": 800})


def test_offline_finding_is_sent_before_completing(browser, tmp_path):
    photo_file = tmp_path / "foto.png"
    photo_file.write_bytes(PNG)
    context, page, errors = field_page(browser)
    wo = pick(page, need_items=True)
    assert wo, "seed_demo no tiene una OT planificada con checklist para el técnico"
    target = next((row for row in wo["items"] if row["type"] == "check"), None)
    assert target, "la OT elegida no tiene un ítem de verificación"

    context.set_offline(True)
    page.evaluate("id => { location.hash = `#/wo/${id}`; }", wo["id"])
    page.wait_for_selector(".field-detail")
    page.click("[data-transition='start']")
    page.wait_for_selector("[data-transition='pause']:not([hidden])")
    for row in wo["items"]:
        if row is target:
            continue
        answer_item(page, row, photo_file)
    li = item(page, target["id"])
    # Sin respuesta (o con OK) no se ofrece el aviso.
    assert li.locator("[data-action='finding-open']").is_hidden()
    li.locator("[data-result='not_ok']").click()
    li = item(page, target["id"])
    li.locator("[data-slot='saved']:not(:empty)").wait_for()
    opener = li.locator("[data-action='finding-open']")
    opener.wait_for()
    finding_shot(page, "button", li)
    opener.click()
    form = li.locator("form[data-form='finding']")
    form.wait_for()
    description = f"Sello dañado e2e {os.getpid()}"
    form.locator("textarea[name='description']").fill(description)
    stopped = form.locator("input[name='asset_stopped']")
    if stopped.is_visible():
        stopped.check()
    finding_shot(page, "form", li)
    form.locator("button[type=submit]").click()
    li = item(page, target["id"])
    state = li.locator(".field-finding__state--warning")
    state.wait_for()
    assert "Hallazgo por enviar" in state.inner_text()
    assert li.locator("[data-action='finding-open']").is_hidden()  # no se avisa dos veces
    finding_shot(page, "pending", li)

    page.click("[data-slot='complete']")
    page.wait_for_selector("form[data-form='complete']")
    page.fill("#fc-labor", "20")
    if page.locator("#fc-meter").is_visible():
        page.fill("#fc-meter", "999999")
    page.click("form[data-form='complete'] button[type=submit]")
    page.wait_for_selector(".field-detail [data-slot='flash']:not([hidden])")

    # El primer envío del hallazgo pierde la respuesta: el reintento no duplica la correctiva.
    seen = set()
    lose_first_response(page, "/finding/", seen)
    context.set_offline(False)
    page.evaluate("window.dispatchEvent(new Event('online'))")
    page.wait_for_selector(".field-sync__row--success", timeout=60000)
    assert seen == {"/finding/"}

    state = manage(FINDING_STATE.format(id=wo["id"], item=target["id"]))
    assert state["status"] in ("completed", "closed")
    assert state["correctives"] == 1 and state["parent"] == [wo["id"]]
    assert state["requests"] == 1  # el técnico no gestiona OT: solicitud + correctiva
    assert description in state["description"][0]
    assert errors == []
    context.close()


def test_synced_finding_shows_its_code_and_cannot_be_repeated(browser):
    context, page, errors = field_page(browser)
    wo = pick(page, need_items=True)
    assert wo, "seed_demo no tiene otra OT planificada con checklist para el técnico"
    target = next((row for row in wo["items"] if row["type"] == "check"), None)
    assert target, "la OT elegida no tiene un ítem de verificación"
    context.set_offline(True)
    page.evaluate("id => { location.hash = `#/wo/${id}`; }", wo["id"])
    page.wait_for_selector(".field-detail")
    item(page, target["id"]).locator("[data-result='not_ok']").click()
    li = item(page, target["id"])
    li.locator("[data-action='finding-open']").click()
    li.locator("form[data-form='finding'] button[type=submit]").click()
    item(page, target["id"]).locator(".field-finding__state--warning").wait_for()

    context.set_offline(False)
    page.evaluate("window.dispatchEvent(new Event('online'))")
    page.wait_for_selector(".field-sync__row--success", timeout=60000)
    state = manage(FINDING_STATE.format(id=wo["id"], item=target["id"]))
    assert state["correctives"] == 1 and state["status"] == "in_progress"
    code = manage(CORRECTIVE_CODE.format(item=target["id"]))
    li = item(page, target["id"])
    done = li.locator(".field-finding__state--success")
    done.wait_for(timeout=10000)
    assert "Hallazgo avisado" in done.inner_text() and code in done.inner_text()
    assert li.locator("[data-action='finding-open']").is_hidden()
    finding_shot(page, "synced", li)
    # Descargar de nuevo: el servidor trae el hallazgo en el snapshot (`finding_code`).
    page.evaluate("() => { location.hash = '#/'; }")
    page.wait_for_selector("[data-action='refresh']")
    with page.expect_response("**/field/api/reference/"):
        page.click("[data-action='refresh']")
    page.wait_for_timeout(1000)
    page.evaluate("id => { location.hash = `#/wo/${id}`; }", wo["id"])
    item(page, target["id"]).locator(".field-finding__state--success", has_text=code).wait_for(timeout=10000)
    assert errors == []
    context.close()


# ---------- Inventario P6: repuestos sin conexión (INV-13) ----------

# Repuesto con stock (10 a $1.000) y reserva de 2 en la OT elegida; lo crea el test, así no
# depende de que seed_demo traiga inventario.
INVENTORY_SETUP = """
import json
from apps.accounts.models import Membership
from apps.core.tenancy import tenant_context
from apps.inventory import services as inventory
from apps.work.models import WorkOrder
wo = WorkOrder.all_tenants.get(pk="{id}")
admin = Membership.objects.filter(organization=wo.organization, role="admin").first()
with tenant_context(wo.organization):
    part = inventory.create_part(membership=admin, code="{code}", name="Rodamiento e2e", unit="un")
    inventory.receive_stock(membership=admin, part=part, quantity=10, unit_cost=1000)
    inventory.reserve_for_work_order(work_order=wo, part=part, quantity=2, source="manual")
    print(json.dumps({{"part": str(part.pk), "parts_cost": float(wo.parts_cost)}}))
"""

INVENTORY_STATE = """
import json
from apps.inventory.models import Reservation, StockItem
from apps.work.models import PartUsage, WorkOrder
wo = WorkOrder.all_tenants.get(pk="{id}")
lines = PartUsage.all_tenants.filter(work_order=wo, part_id="{part}")
usages = list(lines.values_list("quantity", flat=True))
free = PartUsage.all_tenants.filter(work_order=wo, description="{free}").count()
on_hand = float(sum(StockItem.all_tenants.filter(part_id="{part}").values_list("on_hand", flat=True)))
reservation = Reservation.all_tenants.get(work_order=wo, part_id="{part}")
print(json.dumps({{"usages": [float(q) for q in usages], "free": free, "on_hand": on_hand,
    "parts_cost": float(wo.parts_cost), "reservation": reservation.status}}))
"""


def parts_shot(page, name):
    """Captura de la sección Repuestos (E2E_SHOTS) a 360, 1024 y 1440 px."""
    if not os.environ.get("E2E_SHOTS"):
        return
    for width in (360, 1024, 1440):
        page.set_viewport_size({"width": width, "height": 900})
        section = page.locator("[data-slot='parts']")
        section.scroll_into_view_if_needed()
        section.screenshot(path=f"{os.environ['E2E_SHOTS']}/inv6-{name}-{width}.png")
        page.screenshot(path=f"{os.environ['E2E_SHOTS']}/inv6-{name}-{width}-page.png", full_page=True)
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), width
    page.set_viewport_size({"width": 360, "height": 800})


def test_offline_planned_part_is_used_once(browser):
    context, page, errors = field_page(browser)
    wo = pick(page, need_items=False)
    assert wo, "seed_demo no tiene una OT planificada para el técnico"
    code = f"E2E-{os.getpid()}"
    setup = manage(INVENTORY_SETUP.format(id=wo["id"], code=code))
    # "Descargar de nuevo": la OT trae el previsto y `reference`, el catálogo con el repuesto.
    with page.expect_response("**/field/api/reference/"):
        page.click("[data-action='refresh']")
    page.wait_for_timeout(1000)  # se guarda en IndexedDB

    context.set_offline(True)
    page.evaluate("id => { location.hash = `#/wo/${id}`; }", wo["id"])
    planned = page.locator(f"[data-slot='planned'] [data-part='{setup['part']}']")
    planned.wait_for()
    assert code in planned.inner_text()
    assert planned.locator("[data-slot='qty']").input_value() == "2"
    parts_shot(page, "planned")
    planned.locator("[data-action='part-use']").click()
    page.wait_for_selector("[data-slot='parts-saved']:not(:empty)")
    # Usado entero: deja de estar previsto y queda la línea "por enviar".
    assert page.locator(f"[data-slot='planned'] [data-part='{setup['part']}']").count() == 0
    used = page.locator("[data-slot='used']")
    assert code in used.inner_text() and "Por enviar" in used.inner_text()

    # Un material que no está en el catálogo (texto libre).
    free = f"Huaipe e2e {os.getpid()}"
    page.click("[data-action='part-add']")
    page.check("#fp-free")
    page.fill("#fp-description", free)
    page.fill("#fp-qty", "1,5")
    page.fill("#fp-unit", "kg")
    parts_shot(page, "form")
    page.click("form[data-form='part'] button[type=submit]")
    page.locator("[data-slot='used']", has_text=free).wait_for(timeout=5000)
    parts_shot(page, "used")
    assert "Sin conexión" in page.inner_text("#field-sync-rows")

    seen = set()
    lose_first_response(page, "/parts/", seen)
    context.set_offline(False)
    page.evaluate("window.dispatchEvent(new Event('online'))")
    page.wait_for_selector(".field-sync__row--success", timeout=60000)
    assert seen == {"/parts/"}

    state = manage(INVENTORY_STATE.format(id=wo["id"], part=setup["part"], free=free))
    assert state["usages"] == [2.0]  # una sola línea pese al reintento
    assert state["free"] == 1
    assert state["on_hand"] == 8.0
    assert state["reservation"] == "fulfilled"
    assert state["parts_cost"] == setup["parts_cost"] + 2000  # el texto libre va a costo 0
    assert errors == []
    context.close()


# ---------- P5: salir borra los datos del teléfono (ADR-011 §1e) ----------

DATABASES = "async () => (await indexedDB.databases()).map((info) => info.name)"


def shot(page, name):
    if os.environ.get("E2E_SHOTS"):
        page.screenshot(path=f"{os.environ['E2E_SHOTS']}/{name}.png", full_page=True)


def test_exit_from_the_web_with_an_empty_queue_deletes_the_database(browser):
    context, page, errors = field_page(browser)
    assert "galbust-field" in page.evaluate(DATABASES)
    # "Salir" de la web (en móvil está en "Más"): sin cambios pendientes no pregunta nada.
    page.goto("/menu/")
    page.click("#account form[data-field-exit] button[type=submit]")
    page.wait_for_url("**/accounts/login/**")
    assert "galbust-field" not in page.evaluate(DATABASES)
    assert errors == []
    context.close()


def test_exit_with_pending_changes_asks_before_deleting(browser):
    context, page, errors = field_page(browser)
    wo = pick(page, need_items=False)
    assert wo, "seed_demo no tiene una OT planificada para el técnico"
    # El servidor no recibe nada: el cambio queda en la cola.
    page.route(
        "**/field/api/**",
        lambda route: route.abort() if route.request.method == "POST" else route.continue_(),
    )
    page.evaluate("id => { location.hash = `#/wo/${id}`; }", wo["id"])
    page.wait_for_selector(".field-detail")
    page.fill("#fl-minutes", "20")
    page.click("form[data-form='labor'] button[type=submit]")
    page.wait_for_selector("[data-slot='labor-saved']:not(:empty)")

    exit_button = page.locator(".field-bar form[data-field-exit] button[type=submit]")
    exit_button.click()
    dialog = page.locator("#field-exit-dialog")
    dialog.wait_for(state="visible")
    assert "1 cambio de terreno sin enviar" in dialog.inner_text()
    shot(page, "exit-dialog-360")
    dialog.locator("button[value='cancel']").click()
    dialog.wait_for(state="hidden")
    assert "/field/" in page.url
    assert "galbust-field" in page.evaluate(DATABASES)
    pending = page.evaluate(
        """() => new Promise((resolve) => {
            const request = indexedDB.open("galbust-field");
            request.onsuccess = () => {
              const db = request.result;
              const count = db.transaction("queue").objectStore("queue").count();
              count.onsuccess = () => { db.close(); resolve(count.result); };
            };
        })"""
    )
    assert pending == 1

    exit_button.click()
    dialog.wait_for(state="visible")
    dialog.locator("button[value='confirm'][data-mode='exit']").click()
    page.wait_for_url("**/accounts/login/**")
    assert "galbust-field" not in page.evaluate(DATABASES)
    assert errors == []
    context.close()
