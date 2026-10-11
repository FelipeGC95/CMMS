// Entrada de la vista de terreno (ADR-011): módulo ES sin build. Enrutamiento por hash:
//   #/                 OT agrupadas por día (asignadas a la persona primero)
//   #/wo/<id>          ejecución de una OT: iniciar, pausar, checklist, fotos
//   #/wo/<id>/complete completar con minutos, lectura, falla y firma
//   #/request          nueva solicitud (aviso de falla) con foto
//   #/reading          lectura de medidor
//   #/conflicts        cambios con conflicto o rechazados
//   #/fix/<client_uuid> corregir y reenviar un cambio rechazado
// Los datos salen de IndexedDB; se descargan de /field/api/ y los cambios esperan en la cola
// hasta que sync.js los envía.
import * as api from "./api.js";
import * as db from "./db.js";
import * as forms from "./forms.js";
import { applyEntry, normalizeDecimal } from "./local.js";
import * as parts from "./parts.js";
import { PhotoTooLarge, prepare } from "./photos.js";
import * as queue from "./queue.js";
import * as sync from "./sync.js";
import * as ui from "./ui.js";

export const CLIENT_VERSION = api.CLIENT_VERSION;

const body = document.body;
const view = document.getElementById("field-view");
const status = document.getElementById("field-status");
const syncRows = document.getElementById("field-sync-rows");
const updateBanner = document.getElementById("field-update");

const app = {
  session: null,
  identity: null,
  verified: false, // identidad confirmada por `bootstrap` en esta carga
  refreshing: false,
  rendered: "",
  flash: "",
  requestPhoto: null,
  registration: null,
};

// ---------- Utilidades ----------

function route() {
  const raw = location.hash.replace(/^#/, "") || "/";
  const [path, query = ""] = raw.split("?");
  return { path, params: new URLSearchParams(query) };
}

const now = () => new Date().toISOString();

function takeFlash() {
  const message = app.flash;
  app.flash = "";
  return message;
}

function go(hash, flash = "") {
  app.flash = flash;
  if (location.hash === hash) render();
  else location.hash = hash;
}

// Avisa a static/js/signature-pad.js que hay lienzos nuevos (además de htmx:load en la web).
function announce(node) {
  node.dispatchEvent(new CustomEvent("cmms:content", { bubbles: true }));
}

async function loadSession() {
  let session = await db.getSession();
  if (session && db.isStale(session)) {
    await db.clearData();
    session = null;
  }
  app.session = session || null;
  if (session && !app.identity) app.identity = db.identityOf(session.bootstrap);
  return app.session;
}

async function summary() {
  return queue.summarize(await db.listQueue(), app.identity);
}

// ---------- Pantallas ----------

async function updateSyncBar(summaryValue) {
  const value = summaryValue || (await summary());
  ui.renderSync(syncRows, {
    summary: value,
    sync: sync.status(),
    online: navigator.onLine,
    urls: { login: body.dataset.loginUrl, mfa: body.dataset.mfaUrl },
  });
}

async function render({ focus = true } = {}) {
  const session = await loadSession();
  const { path, params } = route();
  const current = await summary();
  await updateSyncBar(current);
  const flash = takeFlash();
  let match;
  view.dataset.dirty = "";
  // Con conexión no se muestra nada guardado hasta que `bootstrap` confirma quién es la persona:
  // en un teléfono compartido, los datos de otra persona u organización se borran antes (§1e).
  if (!session || !app.identity || (app.refreshing && !app.verified)) {
    app.rendered = app.refreshing ? "loading" : "empty";
    ui.renderTemplate(view, app.refreshing ? "tpl-loading" : "tpl-empty");
  } else if ((match = path.match(/^\/wo\/([^/]+)(\/complete)?$/))) {
    const wo = await db.getWorkOrder(decodeURIComponent(match[1]), app.identity);
    const timer = wo ? await db.getTimer(wo.id, app.identity) : null;
    if (!wo) {
      app.rendered = "not-found";
      ui.renderTemplate(view, "tpl-not-found");
    } else if (match[2]) {
      app.rendered = "complete";
      const reference = await db.getReference(app.identity);
      forms.renderComplete(view, { session, wo, timer, reference, entry: null });
    } else {
      app.rendered = "detail";
      ui.renderDetail(view, { session, workOrder: wo, summary: current, timer, flash });
      parts.renderParts(view, { wo, bootstrap: session.bootstrap, reference: await db.getReference(app.identity) });
    }
  } else if (path === "/request") {
    app.rendered = "request";
    app.requestPhoto = null;
    forms.renderRequest(view, { reference: await db.getReference(app.identity), entry: null, params });
  } else if (path === "/reading") {
    app.rendered = "reading";
    forms.renderReading(view, { reference: await db.getReference(app.identity), entry: null, params });
  } else if (path === "/conflicts") {
    app.rendered = "conflicts";
    const entries = current.mine.filter((entry) => queue.PROBLEM_STATUSES.has(entry.status));
    forms.renderConflicts(view, { session, entries, flash });
  } else if ((match = path.match(/^\/fix\/([^/]+)$/))) {
    await renderFix(decodeURIComponent(match[1]), params);
  } else {
    app.rendered = "list";
    ui.renderList(view, {
      session,
      workOrders: await db.listWorkOrders(app.identity),
      summary: current,
      recent: await db.listRecent(app.identity),
      flash,
    });
  }
  announce(view);
  if (focus) ui.focusHeading(view);
}

async function renderFix(clientUuid, params) {
  const entry = await db.getEntry(clientUuid);
  if (!entry || !queue.owns(entry, app.identity) || entry.status !== "rejected") {
    app.rendered = "not-found";
    ui.renderTemplate(view, "tpl-not-found");
    return;
  }
  app.rendered = "fix";
  const reference = await db.getReference(app.identity);
  if (entry.kind === "complete") {
    const wo = await db.getWorkOrder(entry.wo_id, app.identity);
    if (!wo) {
      ui.renderTemplate(view, "tpl-not-found");
      return;
    }
    forms.renderComplete(view, { session: app.session, wo, timer: await db.getTimer(wo.id, app.identity), reference, entry });
  } else if (entry.kind === "request") {
    forms.renderRequest(view, { reference, entry, params });
    const photo = view.querySelector("[data-field='photo']");
    if (photo) photo.hidden = true; // la foto ya guardada se reenvía tal cual
  } else if (entry.kind === "reading") {
    forms.renderReading(view, { reference, entry, params });
  } else if (entry.kind === "part") {
    const wo = await db.getWorkOrder(entry.wo_id, app.identity);
    if (!wo) {
      ui.renderTemplate(view, "tpl-not-found");
      return;
    }
    parts.renderFix(view, { wo, reference, entry });
    if (entry.error) forms.showErrors(view, entry.error.details || { non_field_errors: [entry.error.message] });
  }
  view.querySelector("form").dataset.fix = entry.client_uuid;
}

// Vuelve a pintar solo si no hay nada a medio escribir y la pantalla es una lista.
function rerenderIfSafe() {
  if (view.dataset.dirty) return updateSyncBar();
  if (["list", "conflicts", "empty", "loading"].includes(app.rendered)) return render({ focus: false });
  return updateSyncBar();
}

// ---------- Descarga ----------

function handleApiError(error) {
  if (!(error instanceof api.ApiError)) throw error;
  const kind = error.kind;
  if (kind === "auth" || kind === "mfa" || kind === "upgrade") {
    sync.setPause(kind);
    if (kind === "upgrade") offerUpdate();
  }
}

async function refreshData() {
  const session = await db.getSession();
  if (!session || !app.verified) return;
  const days = session.bootstrap.horizon_days;
  const [snapshot, reference] = await Promise.all([api.get(`snapshot/?days=${days}`), api.get("reference/")]);
  await db.saveSnapshot(snapshot, app.identity);
  await db.saveReference(reference, app.identity);
  await queue.reapply(app.identity, session.bootstrap);
}

// Al abrir y con "Descargar de nuevo": confirma quién es la persona, envía la cola y descarga.
async function refresh() {
  if (app.refreshing || !navigator.onLine) return;
  app.refreshing = true;
  if (!app.verified) await render({ focus: false });
  try {
    const bootstrap = await api.get("bootstrap/");
    if (bootstrap.min_client_version > CLIENT_VERSION) {
      sync.setPause("upgrade");
      offerUpdate();
      return;
    }
    const identity = db.identityOf(bootstrap);
    const previous = await db.getSession();
    // Otra persona u organización en este teléfono: se borran sus datos; su cola queda
    // guardada sin mostrar su contenido hasta que alguien la descarte (ADR-011 §1e).
    if (previous && (previous.user_id !== identity.user_id || previous.organization_id !== identity.organization_id)) {
      await db.clearData();
    }
    await db.saveBootstrap(bootstrap, CLIENT_VERSION);
    app.identity = identity;
    app.verified = true;
    sync.setIdentity(identity);
    if (sync.status().pause) sync.setPause(null);
    await sync.run();
    await refreshData();
  } catch (error) {
    handleApiError(error);
  } finally {
    app.refreshing = false;
    await rerenderIfSafe();
  }
}

let drainedTimer = null;
function afterDrain() {
  clearTimeout(drainedTimer);
  drainedTimer = setTimeout(async () => {
    try {
      await refreshData();
    } catch (error) {
      handleApiError(error);
    }
    await rerenderIfSafe();
  }, 500);
}

async function onSent(entry, data) {
  if (entry.kind === "request" && data) {
    await db.addRecent(
      { number: data.number, work_order_code: data.work_order_code, photo_warning: data.photo_warning || "" },
      app.identity,
    );
    return;
  }
  if (!entry.wo_id || !data) return;
  const wo = await db.getWorkOrder(entry.wo_id, app.identity);
  if (!wo) return;
  if (entry.kind === "answer") {
    // Respuesta del servidor (con `superseded`, la vigente es otra más reciente).
    const item = (wo.checklist || []).find((row) => row.id === entry.item_id);
    if (item) {
      for (const key of ["result", "value_number", "value_text", "note", "answered_at"]) if (key in data) item[key] = data[key];
    }
  } else if (entry.kind === "transition" || entry.kind === "complete") {
    wo.status = data.status;
  } else if (entry.kind === "wo_photo") {
    wo._photo_count = Math.max(0, (wo._photo_count || 0) - 1);
  } else if (entry.kind === "part") {
    // La línea ya está en el servidor; lo que falta usar se actualiza al descargar de nuevo.
    const line = (wo.parts || []).find((row) => row.client_uuid === entry.client_uuid);
    if (line) Object.assign(line, { id: data.id, description: data.description, _pending: false });
  }
  await db.putWorkOrder(wo);
}

// ---------- Acciones de la OT ----------

async function currentWorkOrder() {
  const article = view.querySelector("[data-wo-id]");
  const id = article ? article.dataset.woId : (route().path.match(/^\/wo\/([^/]+)/) || [])[1];
  return id ? db.getWorkOrder(decodeURIComponent(id), app.identity) : null;
}

async function enqueue(options) {
  const entry = await queue.enqueue({ identity: app.identity, session: app.session, ...options });
  sync.run();
  return entry;
}

function keepScroll(task) {
  const y = window.scrollY;
  return task().then(() => window.scrollTo(0, y));
}

async function onTransition(button) {
  const wo = await currentWorkOrder();
  if (!wo) return;
  const rule = app.session.bootstrap.execution_transitions[button.dataset.transition];
  const statusLabel = app.session.bootstrap.status_labels[rule.to] || rule.to;
  await enqueue({ kind: "transition", wo, payload: { to_status: rule.to, at: now() }, label: `${wo.code} · ${statusLabel}` });
  app.flash = ui.msg("savedTransition");
  await keepScroll(() => render({ focus: false }));
  // El botón pulsado ya no existe: el foco pasa a la acción que sigue.
  const next = view.querySelector("[data-slot='actions'] > :not([hidden])");
  if (next) next.focus({ preventScroll: true });
}

function itemOf(wo, li) {
  return (wo.checklist || []).find((row) => row.id === li.dataset.itemId);
}

function itemMessage(li, message, isError = false) {
  const node = li.querySelector("[data-slot='saved']");
  node.classList.toggle("field-saved--error", isError);
  node.replaceChildren(ui.icon(isError ? "stop" : "check"), document.createTextNode(message));
}

// Repinta un ítem tras guardarlo (sin tocar lo que se escribe en los demás).
async function redrawItem(li, wo, message) {
  const fresh = await db.getWorkOrder(wo.id, app.identity);
  const items = fresh.checklist || [];
  const index = items.findIndex((row) => row.id === li.dataset.itemId);
  const current = await summary();
  const fragment = ui.editableItem(items[index], index + 1, current.byItem.get(items[index].id));
  const replacement = fragment.firstElementChild;
  li.replaceWith(fragment);
  ui.setProgress(view, items);
  announce(replacement);
  itemMessage(replacement, message);
  await updateSyncBar(current);
  return replacement;
}

function answerBody(li, item, button) {
  const note = li.querySelector("[data-slot='note']").value.trim();
  const body = { note, answered_at: now() };
  switch (item.item_type) {
    case "measurement": {
      const value = normalizeDecimal(li.querySelector("[data-slot='value']").value);
      if (value === null) return { error: ui.msg("invalidNumber") };
      return { body: { ...body, value_number: value } };
    }
    case "text": {
      const value = li.querySelector("[data-slot='value-text']").value.trim();
      if (!value) return { error: ui.msg("requiredText") };
      return { body: { ...body, value_text: value } };
    }
    case "signature": {
      const value = li.querySelector("[data-slot='signature']").value;
      if (!value) return { error: ui.msg("requiredSignature") };
      return { body: { ...body, signature: value } };
    }
    case "photo":
      return { body };
    default:
      return { body: { ...body, result: button.dataset.result } };
  }
}

// Solo la observación: se reenvía la respuesta vigente del ítem con la nota nueva.
function noteBody(li, item) {
  const body = { note: li.querySelector("[data-slot='note']").value.trim(), answered_at: now() };
  if (item.item_type === "measurement") body.value_number = normalizeDecimal(item.value_number);
  else if (item.item_type === "text") body.value_text = item.value_text;
  else if (item.item_type === "signature") body.signature = item._signature;
  else if (item.item_type !== "photo") body.result = item.result;
  return body;
}

async function onAnswer(button, noteOnly = false) {
  const li = button.closest("[data-item-id]");
  const wo = await currentWorkOrder();
  if (!li || !wo) return;
  const item = itemOf(wo, li);
  if (!item) return;
  const { body, error } = noteOnly ? { body: noteBody(li, item) } : answerBody(li, item, button);
  if (error) {
    itemMessage(li, error, true);
    return;
  }
  await enqueue({ kind: "answer", wo, itemId: item.id, payload: body, label: `${wo.code} · ${item.text}` });
  const replacement = await redrawItem(li, wo, ui.msg(noteOnly ? "savedNote" : "savedAnswer"));
  const target = replacement.querySelector(noteOnly ? "[data-slot='note']" : "[data-action='answer'][aria-pressed='true'], [data-action='answer']");
  if (target) target.focus();
}

async function onPhoto(input) {
  const file = input.files && input.files[0];
  if (!file) return;
  let photo;
  try {
    photo = await prepare(file);
  } catch (error) {
    const message = error instanceof PhotoTooLarge ? ui.msg("photoTooLarge") : String(error.message);
    const li = input.closest("[data-item-id]");
    if (li) itemMessage(li, message, true);
    else {
      const status = view.querySelector("[data-slot='wo-photo-status'], [data-slot='photo-status']");
      if (status) status.textContent = message;
    }
    input.value = "";
    return;
  }
  input.value = "";
  const blobs = [{ blob: photo.blob, name: photo.name, type: photo.type }];
  if (input.dataset.photo === "request") {
    app.requestPhoto = { ...photo };
    view.querySelector("[data-slot='photo-status']").replaceChildren(ui.icon("check"), document.createTextNode(ui.msg("savedPhoto")));
    return;
  }
  const wo = await currentWorkOrder();
  if (!wo) return;
  if (input.dataset.photo === "item") {
    const li = input.closest("[data-item-id]");
    const item = itemOf(wo, li);
    await enqueue({ kind: "item_photo", wo, itemId: item.id, payload: { taken_at: photo.takenAt }, blobs, label: `${wo.code} · ${item.text}` });
    await redrawItem(li, wo, ui.msg("savedPhoto"));
  } else {
    await enqueue({ kind: "wo_photo", wo, payload: { taken_at: photo.takenAt }, blobs, label: wo.code });
    const fresh = await db.getWorkOrder(wo.id, app.identity);
    const count = view.querySelector("[data-slot='wo-photo-count']");
    count.textContent = `${ui.msg("photosPending")}: ${fresh._photo_count || 0}`;
    count.hidden = false;
    view.querySelector("[data-slot='wo-photo-status']").replaceChildren(ui.icon("check"), document.createTextNode(ui.msg("savedPhoto")));
    await updateSyncBar();
  }
}

// ---------- Repuestos (INV-13) ----------

function partsSection() {
  return view.querySelector("[data-slot='parts']");
}

function togglePartForm(open) {
  const section = partsSection();
  if (!section) return;
  const form = section.querySelector("form[data-form='part']");
  const button = section.querySelector("[data-slot='part-add-open']");
  form.hidden = !open;
  button.hidden = open;
  button.setAttribute("aria-expanded", String(open));
  if (open) (form.elements.free.checked ? form.elements.description : form.querySelector("#fp-search")).focus();
  else button.focus();
}

// Encola el repuesto (con la hora real del consumo) y repinta la OT con la línea "por enviar".
async function enqueuePart(wo, payload) {
  const label = `${wo.code} · ${payload.description} · ${payload.quantity}${payload.unit ? ` ${payload.unit}` : ""}`;
  await enqueue({ kind: "part", wo, payload: { ...payload, used_at: now() }, label });
  view.dataset.dirty = "";
  await keepScroll(() => render({ focus: false }));
  const saved = view.querySelector("[data-slot='parts-saved']");
  if (saved) saved.replaceChildren(ui.icon("check"), document.createTextNode(ui.msg("savedPart")));
  const next = view.querySelector("[data-slot='part-add-open']:not([hidden])");
  if (next) next.focus({ preventScroll: true });
}

async function onPartUse(button) {
  const li = button.closest("[data-part]");
  const wo = await currentWorkOrder();
  if (!li || !wo) return;
  const { payload, error } = parts.readUse(li, wo);
  if (error) {
    itemMessage(li, error, true);
    return;
  }
  await enqueuePart(wo, payload);
}

async function onPart(form, fixEntry) {
  const reference = await db.getReference(app.identity);
  const result = parts.readForm(form, reference);
  forms.showErrors(fixEntry ? view : form, result.errors);
  if (Object.keys(result.errors).length) return;
  if (fixEntry) {
    // Se reemplaza el cambio entero (catálogo o texto libre) y se conserva la hora del consumo.
    const entry = await queue.resubmit(fixEntry, { used_at: fixEntry.payload.used_at, ...result.payload });
    const wo = await db.getWorkOrder(entry.wo_id, app.identity);
    if (wo) await db.putWorkOrder(applyEntry(wo, entry, app.session.bootstrap));
    sync.run();
    go(`#/wo/${encodeURIComponent(entry.wo_id)}`, ui.msg("savedFix"));
    return;
  }
  const wo = await currentWorkOrder();
  if (wo) await enqueuePart(wo, result.payload);
}

// ---------- Formularios ----------

// Horas de apoyo: se quedan en la OT (no cambian su estado) y se envían a `labor/`.
async function onLabor(form) {
  const wo = await currentWorkOrder();
  if (!wo) return;
  const result = forms.readLabor(form);
  forms.showErrors(form, result.errors);
  if (Object.keys(result.errors).length) return;
  const label = `${wo.code} · ${result.payload.minutes} ${ui.msg("minutes")}`;
  await enqueue({ kind: "labor", wo, payload: result.payload, label });
  form.reset();
  view.dataset.dirty = "";
  form.querySelector("[data-slot='labor-saved']").replaceChildren(ui.icon("check"), document.createTextNode(ui.msg("savedLabor")));
  await updateSyncBar();
}

async function onSubmit(form) {
  const kind = form.dataset.form;
  if (kind === "labor") {
    await onLabor(form);
    return;
  }
  const fixUuid = form.dataset.fix;
  const fixEntry = fixUuid ? await db.getEntry(fixUuid) : null;
  if (kind === "part") {
    await onPart(form, fixEntry);
    return;
  }
  let result;
  let wo = null;
  if (kind === "complete") {
    wo = fixEntry ? await db.getWorkOrder(fixEntry.wo_id, app.identity) : await currentWorkOrder();
    if (!wo) return;
    result = forms.readComplete(form, wo);
    // Un cierre ya enviado una vez no revisa el checklist local (puede estar desactualizado).
    if (fixEntry) delete result.errors.non_field_errors;
  } else if (kind === "request") {
    result = forms.readRequest(form);
  } else {
    result = forms.readReading(form);
  }
  if (Object.keys(result.errors).length) {
    forms.showErrors(view, result.errors);
    return;
  }
  if (fixEntry) {
    await queue.resubmit(fixEntry, { ...fixEntry.payload, ...result.payload });
    sync.run();
    go(fixEntry.wo_id ? `#/wo/${encodeURIComponent(fixEntry.wo_id)}` : "#/", ui.msg("savedFix"));
    return;
  }
  if (kind === "complete") {
    await enqueue({ kind: "complete", wo, payload: { ...result.payload, at: now() }, label: wo.code });
    go(`#/wo/${encodeURIComponent(wo.id)}`, ui.msg("savedComplete"));
  } else if (kind === "request") {
    const photo = app.requestPhoto;
    const blobs = photo ? [{ blob: photo.blob, name: photo.name, type: photo.type }] : [];
    const label = result.payload.description.slice(0, 60);
    await enqueue({ kind: "request", payload: { ...result.payload, reported_at: now() }, blobs, label });
    app.requestPhoto = null;
    go("#/", ui.msg("savedRequest"));
  } else {
    const select = form.elements.meter;
    const label = select.options[select.selectedIndex] ? select.options[select.selectedIndex].textContent : "";
    await enqueue({ kind: "reading", payload: { ...result.payload, read_at: now() }, label });
    go("#/", ui.msg("savedReading"));
  }
}

async function onDiscard(button) {
  if (!window.confirm(ui.msg("confirmDiscard"))) return;
  const card = button.closest("[data-client-uuid]");
  const entry = await db.getEntry(card.dataset.clientUuid);
  if (entry && queue.owns(entry, app.identity)) await queue.discard(entry);
  app.flash = "";
  await render({ focus: false });
  // Lo que seguía de esa OT ya puede enviarse; luego se descarga de nuevo.
  await sync.run();
  afterDrain();
}

async function onDiscardOthers() {
  if (!window.confirm(ui.msg("confirmDiscardOthers"))) return;
  for (const entry of await db.listQueue()) {
    if (!(app.identity && entry.user_id === app.identity.user_id)) await queue.discard(entry);
  }
  await updateSyncBar();
}

// ---------- Versión nueva del Service Worker ----------

function offerUpdate() {
  if (app.registration) app.registration.update().catch(() => {});
  updateBanner.hidden = false;
}

async function applyUpdate() {
  // No se corta un envío en curso: se espera a que termine.
  while (sync.isRunning()) await new Promise((resolve) => setTimeout(resolve, 300));
  const registration = app.registration;
  if (registration && registration.waiting) registration.waiting.postMessage({ type: "SKIP_WAITING" });
  else location.reload();
}

function watchUpdates(registration) {
  app.registration = registration;
  const offer = () => {
    if (registration.waiting && navigator.serviceWorker.controller) updateBanner.hidden = false;
  };
  offer();
  registration.addEventListener("updatefound", () => {
    const worker = registration.installing;
    if (worker) worker.addEventListener("statechange", offer);
  });
  let reloading = false;
  navigator.serviceWorker.addEventListener("controllerchange", () => {
    if (!reloading) {
      reloading = true;
      location.reload();
    }
  });
}

async function registerServiceWorker() {
  if (!("serviceWorker" in navigator)) return;
  try {
    const registration = await navigator.serviceWorker.register(body.dataset.swUrl, { scope: body.dataset.scope });
    watchUpdates(registration);
  } catch (error) {
    // Sin Service Worker (navegación privada, http): la vista funciona igual mientras haya red.
  }
  if (navigator.storage && navigator.storage.persist) navigator.storage.persist().catch(() => {});
}

// ---------- Eventos ----------

function showConnection() {
  ui.renderConnection(status, navigator.onLine);
  updateSyncBar();
}

document.addEventListener("click", (event) => {
  const target = event.target.closest("[data-action]");
  if (!target) return;
  const action = target.dataset.action;
  if (action === "update") applyUpdate();
  else if (action === "sync") sync.run();
  else if (action === "discard-others") onDiscardOthers();
  else if (action === "refresh") refresh();
  else if (action === "transition") onTransition(target);
  else if (action === "answer") onAnswer(target);
  else if (action === "note") onAnswer(target, true);
  else if (action === "discard") onDiscard(target);
  else if (action === "part-add") togglePartForm(true);
  else if (action === "part-cancel") togglePartForm(false);
  else if (action === "part-use") onPartUse(target);
});

view.addEventListener("submit", (event) => {
  const form = event.target.closest("form[data-form]");
  if (!form) return;
  event.preventDefault();
  onSubmit(form);
});

view.addEventListener("change", (event) => {
  const input = event.target;
  if (input.matches("input[type='file'][data-photo]")) onPhoto(input);
  else if (input.matches("#fm-meter")) db.getReference(app.identity).then((reference) => forms.describeMeter(view, reference));
  else if (input.matches("#fp-free, #fp-part")) {
    db.getReference(app.identity).then((reference) => parts.showMode(input.form, reference));
  }
});

view.addEventListener("input", async (event) => {
  view.dataset.dirty = "1";
  const input = event.target;
  if (input.matches("[data-filter='fp-part']")) {
    const reference = await db.getReference(app.identity);
    const select = document.getElementById("fp-part");
    parts.fillPartOptions(select, parts.partRows(reference), input.value);
    parts.showMode(select.form, reference);
  } else if (input.matches("[data-filter]")) {
    const reference = await db.getReference(app.identity);
    const rows = input.dataset.filter === "fm-meter" ? forms.meterRows(reference) : forms.assetRows(reference);
    forms.applyFilter(input, rows);
    if (input.dataset.filter === "fm-meter") forms.describeMeter(view, reference);
  }
});

sync.configure({
  onChange: () => updateSyncBar(),
  onSent,
  onDrained: afterDrain,
  // La sesión cambió de organización (otra pestaña): se confirma de nuevo quién es antes de
  // seguir enviando; los cambios de la organización anterior esperan en la cola.
  onIdentityChanged: () => {
    app.verified = false;
    setTimeout(refresh, 0);
  },
});

window.addEventListener("hashchange", () => render());
window.addEventListener("online", () => {
  showConnection();
  if (!app.verified) refresh();
});
window.addEventListener("offline", showConnection);

api.configure(body.dataset.apiUrl);
sync.start();
showConnection();
if (navigator.onLine) refresh();
else render();
registerServiceWorker();
