// Pantallas de la vista de terreno. Todo se arma clonando <template> de field/shell.html y
// escribiendo con textContent: nunca se interpreta HTML (los datos vienen del servidor o del
// teléfono y podrían traer marcas).

import {
  LABOR_LOCKED_STATUSES,
  LOCKED_STATUSES,
  PROBLEM_STATUSES,
  canAnswer,
  editableDecimal,
  executionActions,
  workedMinutes,
} from "./local.js";

const STATUS_TONES = {
  requested: "warning",
  planned: "neutral",
  scheduled: "info",
  in_progress: "info",
  on_hold: "warning",
  completed: "success",
  closed: "success",
  cancelled: "neutral",
};
const TONE_ICONS = { success: "check", warning: "wrench", error: "stop", info: "info", neutral: "dot" };
const RESULT_TONES = { ok: "success", done: "success", not_ok: "error", na: "neutral" };
const PRIORITY_TONES = { 1: "error", 2: "warning" };
const OPEN_STATUSES = new Set(["in_progress", "on_hold"]);

const text = document.getElementById("field-text").dataset;
const labels = document.getElementById("field-labels");

// data-msg-list-title → msg("listTitle")
export const msg = (key) => text[`msg${key.charAt(0).toUpperCase()}${key.slice(1)}`] || key;

// Plural con dos textos: data-msg-x-one y data-msg-x-other ("{n} …").
export const plural = (key, n) => (n === 1 ? msg(`${key}One`) : msg(`${key}Other`).replace("{n}", String(n)));

export function label(kind, value) {
  const node = labels.querySelector(`[data-${kind}="${CSS.escape(String(value))}"]`);
  return node ? node.textContent : String(value);
}

export function choices(kind) {
  return [...labels.querySelectorAll(`[data-${kind}]`)].map((node) => [node.dataset[kind], node.textContent]);
}

export function icon(name) {
  const tpl = document.getElementById(`icon-${name}`);
  return tpl.content.firstElementChild.cloneNode(true);
}

export function clone(id) {
  return document.getElementById(id).content.cloneNode(true);
}

export function slot(root, name) {
  return root.querySelector(`[data-slot="${name}"]`);
}

export function badge(tone, iconName, textValue) {
  const span = document.createElement("span");
  span.className = `badge badge--${tone}`;
  span.append(icon(iconName), document.createTextNode(textValue));
  return span;
}

export function statusBadge(status, bootstrap) {
  const tone = STATUS_TONES[status] || "neutral";
  const name = (bootstrap.status_labels && bootstrap.status_labels[status]) || status;
  return badge(tone, TONE_ICONS[tone], name);
}

function priorityBadge(priority) {
  const tone = PRIORITY_TONES[priority];
  return tone ? badge(tone, "alert", label("priority", priority)) : null;
}

// Insignias de la cola: "Por enviar: 2" y "Con conflicto". Llevan `data-queue-badge` para
// cambiarlas en vivo (updateQueue) sin repintar el ítem ni la OT.
function queueBadges(counts) {
  const out = [];
  if (counts && counts.pending) out.push(badge("warning", "clock", `${msg("badgePending")}: ${counts.pending}`));
  if (counts && counts.problems) out.push(badge("error", "stop", msg("badgeProblem")));
  out.forEach((node) => {
    node.dataset.queueBadge = "";
  });
  return out;
}

function show(node, value) {
  if (!node) return;
  if (value === undefined || value === null || value === false || value === "") {
    node.hidden = true;
  } else {
    if (typeof value === "string") node.textContent = value;
    node.hidden = false;
  }
}

export function setFlash(root, message) {
  const node = slot(root, "flash");
  if (node && message) {
    node.replaceChildren(icon("check"), document.createTextNode(message));
    node.hidden = false;
  }
}

// ---------- Fechas (es-CL, en la zona de la organización) ----------

function todayIn(timeZone) {
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).formatToParts(new Date());
  const get = (type) => parts.find((part) => part.type === type).value;
  return `${get("year")}-${get("month")}-${get("day")}`;
}

// Una fecha sin hora ("2026-09-28") se formatea en UTC para que no cambie de día.
function formatDay(isoDate, options) {
  return new Intl.DateTimeFormat("es-CL", { timeZone: "UTC", ...options }).format(new Date(`${isoDate}T12:00:00Z`));
}

function formatDayTitle(isoDate) {
  const value = formatDay(isoDate, { weekday: "long", day: "numeric", month: "long" });
  return value.charAt(0).toUpperCase() + value.slice(1);
}

function formatShortDay(isoDate) {
  return formatDay(isoDate, { day: "numeric", month: "short" });
}

export function formatDateTime(iso, timeZone) {
  return new Intl.DateTimeFormat("es-CL", { timeZone, dateStyle: "medium", timeStyle: "short" }).format(new Date(iso));
}

export function formatNumber(value) {
  return new Intl.NumberFormat("es-CL", { maximumFractionDigits: 3 }).format(Number(value));
}

// ---------- Lista de OT agrupada por día ----------

function dayKey(wo, today) {
  const date = wo.scheduled_for || wo.due_on;
  if (!date) return OPEN_STATUSES.has(wo.status) ? today : "none";
  return date < today ? "overdue" : date;
}

function compareWorkOrders(a, b) {
  if (a.assigned_to_me !== b.assigned_to_me) return a.assigned_to_me ? -1 : 1;
  if (a.priority !== b.priority) return a.priority - b.priority;
  return a._position - b._position;
}

export function groupByDay(workOrders, today) {
  const groups = new Map();
  for (const wo of workOrders) {
    const key = dayKey(wo, today);
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(wo);
  }
  const rank = (key) => (key === "overdue" ? "0" : key === "none" ? "9" : `1${key}`);
  return [...groups.entries()]
    .sort(([a], [b]) => rank(a).localeCompare(rank(b)))
    .map(([key, items]) => [key, items.sort(compareWorkOrders)]);
}

function targetText(wo) {
  if (wo.asset) return `${wo.asset.code} · ${wo.asset.name}`;
  if (wo.location) return wo.location.path || wo.location.name;
  return "";
}

function fact(iconName, value) {
  const span = document.createElement("span");
  span.className = "field-wo__fact";
  span.append(icon(iconName), document.createTextNode(value));
  return span;
}

function workOrderItem(wo, bootstrap, counts) {
  const fragment = clone("tpl-wo-item");
  const link = slot(fragment, "link");
  link.href = `#/wo/${encodeURIComponent(wo.id)}`;
  if (wo.assigned_to_me) link.parentElement.classList.add("field-wo--mine");
  slot(fragment, "code").textContent = wo.code;
  slot(fragment, "title").textContent = wo.title;
  const meta = slot(fragment, "meta");
  const target = targetText(wo);
  if (target) meta.append(fact("box", target));
  if (wo.due_on) meta.append(fact("calendar", `${msg("dueOn")}: ${formatShortDay(wo.due_on)}`));
  const badges = slot(fragment, "badges");
  badges.dataset.queueWo = wo.id;
  badges.append(statusBadge(wo.status, bootstrap));
  const priority = priorityBadge(wo.priority);
  if (priority) badges.append(priority);
  if (wo.assigned_to_me) badges.append(badge("info", "user", msg("assignedToMe")));
  badges.append(...queueBadges(counts));
  return fragment;
}

function applyPermissions(root, bootstrap) {
  root.querySelectorAll("[data-perm]").forEach((node) => {
    node.hidden = !bootstrap.permissions[node.dataset.perm];
  });
}

export function renderList(view, { session, workOrders, summary, recent, flash }) {
  const { bootstrap } = session;
  const timeZone = bootstrap.organization.timezone;
  const today = todayIn(timeZone);
  const fragment = clone("tpl-list");
  slot(fragment, "title").textContent = msg("listTitle");
  const synced = session.synced_at ? ` · ${msg("syncedAt")} ${formatDateTime(session.synced_at, timeZone)}` : "";
  slot(fragment, "summary").textContent = `${workOrders.length} ${msg("workOrders")}${synced}`;
  setFlash(fragment, flash);
  applyPermissions(fragment, bootstrap);
  slot(fragment, "conflicts").hidden = !summary.problems;
  if (session.truncated) show(slot(fragment, "truncated"), msg("truncated"));
  if (recent.length) {
    const list = slot(fragment, "recent-list");
    for (const item of recent) {
      const li = document.createElement("li");
      li.append(icon("check"), document.createTextNode(` ${msg("requestNumber")} ${item.number}`));
      if (item.work_order_code) li.append(document.createTextNode(` · ${item.work_order_code}`));
      if (item.photo_warning) li.append(document.createTextNode(` · ${item.photo_warning}`));
      list.append(li);
    }
    slot(fragment, "recent").hidden = false;
  }
  const days = slot(fragment, "days");
  for (const [key, items] of groupByDay(workOrders, today)) {
    const day = clone("tpl-day");
    const title = slot(day, "title");
    if (key === "overdue") {
      title.append(icon("alert"), document.createTextNode(msg("overdue")));
    } else if (key === "none") {
      title.textContent = msg("noDate");
    } else {
      title.textContent = formatDayTitle(key);
      if (key === today) title.append(document.createTextNode(" "), badge("info", "calendar", msg("today")));
    }
    const list = slot(day, "items");
    items.forEach((wo) => list.append(workOrderItem(wo, bootstrap, summary.byWorkOrder.get(wo.id))));
    days.append(day);
  }
  slot(fragment, "none").hidden = workOrders.length > 0;
  view.replaceChildren(fragment);
}

// ---------- Detalle y ejecución de una OT ----------

function addFact(facts, labelText, value, iconName) {
  if (value === null || value === undefined || value === "") return;
  const row = clone("tpl-fact");
  slot(row, "label").textContent = labelText;
  const dd = slot(row, "value");
  if (iconName) dd.append(icon(iconName));
  dd.append(document.createTextNode(value));
  facts.append(row);
}

function itemBadge(item) {
  if (!item.result) {
    return item.required ? badge("warning", "clock", msg("pending")) : badge("neutral", "dot", msg("optional"));
  }
  const tone = RESULT_TONES[item.result] || "neutral";
  return badge(tone, TONE_ICONS[tone], label("result", item.result));
}

function rangeText(item) {
  if (item.min_value === null && item.max_value === null) return "";
  const low = item.min_value === null ? "…" : formatNumber(item.min_value);
  const high = item.max_value === null ? "…" : formatNumber(item.max_value);
  return `${msg("range")}: ${low} – ${high}${item.unit ? ` ${item.unit}` : ""}`;
}

function itemHint(item) {
  const parts = [];
  if (item.value_number !== null && item.value_number !== undefined && item.value_number !== "") {
    parts.push(`${formatNumber(item.value_number)}${item.unit ? ` ${item.unit}` : ""}`);
  }
  if (item.value_text) parts.push(item.value_text);
  const range = rangeText(item);
  if (range) parts.push(range);
  if (item.photo_count) parts.push(`${msg("photos")}: ${item.photo_count}`);
  return parts.join(" · ");
}

// Hallazgo del ítem (WO-06 CA2): "por enviar" mientras espera en la cola; "no enviado" si el
// servidor lo rechazó (se corrige o descarta en Conflictos, sin detener la OT); "avisado" con el
// código de la correctiva (y la solicitud, si pasó por el aviso de falla) cuando el servidor lo creó.
// `entry` es el aviso en la cola para este ítem (summary.findings), si hay.
export function findingStatus(item, entry = null) {
  if (entry && PROBLEM_STATUSES.has(entry.status)) {
    return { tone: "error", icon: "stop", text: msg("findingProblem"), link: "#/conflicts" };
  }
  if (entry || item._finding_pending) return { tone: "warning", icon: "clock", text: msg("findingPending") };
  if (!item.finding_code) return null;
  const number = item.finding_request_number;
  const request = number ? ` · ${msg("requestNumber")} ${number}` : "";
  return { tone: "success", icon: "check", text: `${msg("findingDone")} · ${item.finding_code}${request}` };
}

function showFinding(node, item, entry = null) {
  const state = findingStatus(item, entry);
  if (!state) {
    node.hidden = true;
    return false;
  }
  node.className = `field-finding__state field-finding__state--${state.tone}`;
  const textNode = document.createElement("span");
  textNode.textContent = state.text;
  if (state.link) {
    const link = document.createElement("a");
    link.href = state.link;
    link.textContent = msg("actionReview");
    textNode.append(" ", link);
  }
  node.replaceChildren(icon(state.icon), textNode);
  node.hidden = false;
  return true;
}

// Al cambiar el hallazgo con la OT a la vista (enviado o rechazado): se actualiza solo su estado
// (sin repintar el ítem, así no se pierde lo que se esté escribiendo).
export function updateFinding(li, item, entry = null) {
  const node = slot(li, "finding-state");
  if (!node || !showFinding(node, item, entry)) return;
  const box = slot(li, "finding");
  if (box) box.hidden = false;
  const opener = slot(li, "finding-open");
  if (opener) opener.hidden = true;
  const saved = slot(li, "saved");
  if (saved && !entry) saved.replaceChildren(); // "guardado en el teléfono" ya no aplica
}

// Cambios de la cola con la lista o una OT a la vista (encolar, enviar, rechazar, descartar):
// insignias "Por enviar: N" de cada OT e ítem, el aviso de conflictos y el estado de los
// hallazgos en cola, sin repintar nada más (lo que se esté escribiendo no se pierde).
export function updateQueue(view, summary) {
  view.querySelectorAll("[data-queue-item], [data-queue-wo]").forEach((node) => {
    const { queueItem, queueWo } = node.dataset;
    const counts = queueItem ? summary.byItem.get(queueItem) : summary.byWorkOrder.get(queueWo);
    node.querySelectorAll(":scope > [data-queue-badge]").forEach((old) => old.remove());
    node.append(...queueBadges(counts));
    const entry = queueItem && summary.findings.get(queueItem);
    if (entry) updateFinding(node.closest("li"), {}, entry);
  });
  const article = view.querySelector("[data-wo-id]");
  const problem = slot(view, "problem");
  if (article && problem) {
    const counts = summary.byWorkOrder.get(article.dataset.woId);
    problem.hidden = !(counts && counts.problems);
  }
  const conflicts = slot(view, "conflicts");
  if (conflicts) conflicts.hidden = !summary.problems;
}

// Variable de condición que alimenta un ítem de medición (CBM-06): «Alimenta Temperatura (°C) · Rodamiento».
function showVariable(node, item) {
  if (node && item.variable) show(node, `${msg("feedsVariable")} ${item.variable}`);
}

// "Avisar hallazgo" solo en un ítem No OK (verificación o medición fuera de rango), una vez, y
// con la OT abierta (como en la web; un aviso ya en cola sí puede llegar después de completar).
export function canReportFinding(item, wo, bootstrap, entry = null) {
  return (
    item.result === "not_ok" &&
    ["check", "measurement"].includes(item.item_type) &&
    Boolean(bootstrap.permissions.request_create) &&
    !LOCKED_STATUSES.has(wo.status) &&
    !findingStatus(item, entry)
  );
}

// Insignias del ítem: su resultado y, aparte (para cambiarlas en vivo), las de la cola.
function itemBadges(fragment, item, counts) {
  const node = slot(fragment, "badges");
  node.dataset.queueItem = item.id;
  node.append(itemBadge(item), ...queueBadges(counts));
}

function readOnlyItem(item, index, counts, finding) {
  const fragment = clone("tpl-check-item");
  const li = fragment.firstElementChild;
  li.id = `item-${item.id}`;
  if (item.result) li.classList.add(`check-item--${item.result}`);
  slot(fragment, "num").textContent = `${index}.`;
  slot(fragment, "text").textContent = item.text;
  itemBadges(fragment, item, counts);
  show(slot(fragment, "hint"), itemHint(item));
  show(slot(fragment, "note"), item.note);
  showVariable(slot(fragment, "variable"), item);
  showFinding(slot(fragment, "finding-state"), item, finding);
  return fragment;
}

// Un id único por ítem para enlazar etiquetas y campos.
function bind(root, labelSlot, fieldSlot, id) {
  const field = slot(root, fieldSlot);
  const labelNode = slot(root, labelSlot);
  field.id = id;
  if (labelNode.tagName === "LABEL") labelNode.htmlFor = id;
  else labelNode.id = `${id}-label`;
  return field;
}

// `queued`: lo que el ítem tiene en la cola ({counts, finding}, de queue.summarize).
export function editableItem(item, index, queued, wo, bootstrap) {
  const { counts, finding } = queued || {};
  const fragment = clone("tpl-check-edit");
  const li = fragment.firstElementChild;
  li.dataset.itemId = item.id;
  li.id = `item-${item.id}`;
  if (item.result) li.classList.add(`check-item--${item.result}`);
  slot(fragment, "num").textContent = `${index}.`;
  slot(fragment, "text").textContent = item.text;
  itemBadges(fragment, item, counts);
  const type = ["measurement", "text", "signature", "photo"].includes(item.item_type) ? item.item_type : "check";
  fragment.querySelectorAll("[data-type]").forEach((node) => {
    if (node.dataset.type !== type) node.remove();
  });
  if (type === "check") {
    fragment.querySelectorAll("[data-result]").forEach((button) => {
      button.setAttribute("aria-pressed", String(button.dataset.result === item.result));
    });
  } else if (type === "measurement") {
    const input = bind(fragment, "value-label", "value", `value-${item.id}`);
    input.value = editableDecimal(item.value_number);
    slot(fragment, "unit").textContent = item.unit || "";
    const range = slot(fragment, "range");
    const parts = [rangeText(item)].filter(Boolean);
    range.replaceChildren(document.createTextNode(parts.join("")));
    if (item.result === "ok" || item.result === "not_ok") {
      range.append(
        document.createTextNode(" "),
        item.result === "ok" ? badge("success", "check", msg("inRange")) : badge("error", "stop", msg("outOfRange")),
      );
    }
    range.hidden = !range.textContent.trim();
    showVariable(slot(fragment, "variable"), item);
  } else if (type === "text") {
    bind(fragment, "text-label", "value-text", `text-${item.id}`).value = item.value_text || "";
  } else if (type === "signature") {
    const canvas = fragment.querySelector("[data-signature-canvas]");
    slot(fragment, "sign-label").id = `sign-${item.id}-label`;
    canvas.setAttribute("aria-labelledby", `sign-${item.id}-label`);
    if (item._signature) slot(fragment, "signature").value = item._signature;
  } else if (type === "photo") {
    const state = slot(fragment, "photo-state");
    state.append(item.photo_count ? badge("success", "check", msg("photoReady")) : badge("warning", "camera", msg("photoMissing")));
  }
  bind(fragment, "note-label", "note", `note-${item.id}`).value = item.note || "";
  // La observación sola se envía con una respuesta ya dada (o con la foto ya tomada).
  slot(fragment, "note-save").hidden = !(item.result && (type !== "signature" || item._signature));
  const photo = bind(fragment, "photo-label", "photo-input", `photo-${item.id}`);
  photo.dataset.itemId = item.id;
  const reported = showFinding(slot(fragment, "finding-state"), item, finding);
  const canReport = canReportFinding(item, wo, bootstrap, finding);
  slot(fragment, "finding").hidden = !(reported || canReport);
  slot(fragment, "finding-open").hidden = !canReport;
  if (canReport) {
    const form = slot(fragment, "finding-form");
    form.id = `finding-${item.id}`;
    slot(fragment, "finding-open").setAttribute("aria-controls", form.id);
    bind(fragment, "finding-description-label", "finding-description", `finding-description-${item.id}`);
    bind(fragment, "finding-stopped-label", "finding-stopped", `finding-stopped-${item.id}`);
    // "Equipo detenido" solo aplica a un activo (una ubicación no tiene estado que detener).
    slot(fragment, "finding-stopped-field").hidden = !wo.asset;
  }
  return fragment;
}

export function renderDetail(view, { session, workOrder: wo, summary, timer, flash }) {
  const { bootstrap } = session;
  const fragment = clone("tpl-detail");
  const article = fragment.firstElementChild;
  article.dataset.woId = wo.id;
  slot(fragment, "code").textContent = wo.code;
  slot(fragment, "title").textContent = wo.title;
  const counts = summary.byWorkOrder.get(wo.id);
  const badges = slot(fragment, "badges");
  badges.dataset.queueWo = wo.id;
  badges.append(statusBadge(wo.status, bootstrap));
  const priority = priorityBadge(wo.priority);
  if (priority) badges.append(priority);
  if (wo.assigned_to_me) badges.append(badge("info", "user", msg("assignedToMe")));
  badges.append(...queueBadges(counts));
  setFlash(fragment, flash);
  slot(fragment, "problem").hidden = !(counts && counts.problems);

  // Ejecución: iniciar, pausar, reanudar y completar.
  const actions = executionActions(wo, bootstrap);
  fragment.querySelectorAll("[data-transition]").forEach((button) => {
    button.hidden = !actions.includes(button.dataset.transition);
  });
  const complete = slot(fragment, "complete");
  complete.href = `#/wo/${encodeURIComponent(wo.id)}/complete`;
  complete.hidden = !actions.includes("complete");
  const minutes = workedMinutes(timer);
  if (minutes > 0) show(slot(fragment, "worked"), `${msg("worked")}: ${minutes} ${msg("minutes")}`);
  if (!bootstrap.permissions.execute) show(slot(fragment, "exec-note"), msg("readOnly"));
  else if (LOCKED_STATUSES.has(wo.status)) show(slot(fragment, "exec-note"), msg("locked"));
  if (!actions.length && slot(fragment, "exec-note").hidden && slot(fragment, "worked").hidden) {
    slot(fragment, "exec").hidden = true;
  }
  // Horas de apoyo: hasta que la OT se cierra o se cancela (como en la web).
  slot(fragment, "labor").hidden = !(bootstrap.permissions.execute && !LABOR_LOCKED_STATUSES.has(wo.status));

  const facts = slot(fragment, "facts");
  addFact(facts, msg("target"), targetText(wo), "box");
  addFact(facts, msg("scheduledFor"), wo.scheduled_for && formatDayTitle(wo.scheduled_for), "calendar");
  addFact(facts, msg("dueOn"), wo.due_on && formatDayTitle(wo.due_on), "calendar");
  addFact(facts, msg("assignee"), wo.assigned_to && wo.assigned_to.name, "user");
  addFact(facts, msg("estimate"), wo.estimated_minutes && `${wo.estimated_minutes} ${msg("minutes")}`, "clock");
  if (wo.meter) {
    const required = wo.meter_required ? ` · ${msg("meterRequired")}` : "";
    addFact(facts, msg("meter"), `${wo.meter.name || msg("meter")} (${wo.meter.unit})${required}`, "clock");
  }
  show(slot(fragment, "description"), wo.description);
  if (wo.asset && bootstrap.permissions.request_create) {
    const link = slot(fragment, "link-request");
    link.href = `#/request?asset=${encodeURIComponent(wo.asset.id)}`;
    link.hidden = false;
  }
  if (wo.meter && bootstrap.permissions.reading_create) {
    const link = slot(fragment, "link-reading");
    link.href = `#/reading?meter=${encodeURIComponent(wo.meter.id)}`;
    link.hidden = false;
  }

  const editable = canAnswer(wo, bootstrap);
  const list = slot(fragment, "checklist");
  const items = wo.checklist || [];
  let section = null;
  items.forEach((item, index) => {
    if (item.section && item.section !== section) {
      const header = clone("tpl-check-section");
      slot(header, "title").textContent = item.section;
      list.append(header);
    }
    section = item.section;
    const queued = { counts: summary.byItem.get(item.id), finding: summary.findings.get(item.id) };
    list.append(
      editable
        ? editableItem(item, index + 1, queued, wo, bootstrap)
        : readOnlyItem(item, index + 1, queued.counts, queued.finding),
    );
  });
  if (items.length) {
    setProgress(fragment, items);
  } else {
    slot(fragment, "checklist-empty").hidden = false;
  }

  if (editable) {
    slot(fragment, "wo-photos").hidden = false;
    bind(fragment, "wo-photo-label", "wo-photo-input", `photo-wo-${wo.id}`);
    if (wo._photo_count) show(slot(fragment, "wo-photo-count"), `${msg("photosPending")}: ${wo._photo_count}`);
  }
  view.replaceChildren(fragment);
}

export function setProgress(root, items) {
  const answered = items.filter((item) => item.result).length;
  slot(root, "progress").textContent = `${answered} ${msg("of")} ${items.length} ${msg("itemsDone")}`;
}

export function renderTemplate(view, id) {
  view.replaceChildren(clone(id));
}

export function renderConnection(node, online) {
  node.replaceChildren(icon(online ? "check" : "x"), document.createTextNode(msg(online ? "online" : "offline")));
  node.dataset.online = online ? "true" : "false";
}

// ---------- Barra de sincronización ----------

function syncRow(tone, iconName, message, action) {
  const row = document.createElement("p");
  row.className = `field-sync__row field-sync__row--${tone}`;
  const textNode = document.createElement("span");
  textNode.className = "field-sync__text";
  textNode.textContent = message;
  row.append(icon(iconName), textNode);
  if (action) row.append(action);
  return row;
}

function actionLink(textValue, href) {
  const link = document.createElement("a");
  link.className = "btn btn--ghost field-sync__action";
  link.href = href;
  link.textContent = textValue;
  return link;
}

function actionButton(textValue, action) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "btn btn--ghost field-sync__action";
  button.dataset.action = action;
  button.textContent = textValue;
  return button;
}

// Filas de la barra según la cola y el estado del envío (ADR-011 §1a y §1e).
export function renderSync(container, { summary, sync, online, urls, loggedOut }) {
  const rows = [];
  const pending = summary.pending;
  if (sync.pause === "auth" || loggedOut) {
    const message = pending ? plural("syncAuth", pending) : msg("syncAuthNone");
    rows.push(syncRow("warning", "key", message, actionLink(msg("actionLogin"), urls.login)));
  } else if (sync.pause === "mfa") {
    rows.push(syncRow("warning", "key", msg("syncMfa"), actionLink(msg("actionMfa"), urls.mfa)));
  } else if (sync.pause === "upgrade") {
    rows.push(syncRow("warning", "sync", msg("syncUpgrade"), actionButton(msg("actionUpdate"), "update")));
  } else if (pending && !online) {
    rows.push(syncRow("warning", "x", plural("syncOffline", pending)));
  } else if (sync.running && pending) {
    rows.push(syncRow("info", "sync", msg("syncSending")));
  } else if (pending) {
    rows.push(syncRow("info", "clock", plural("syncPending", pending), actionButton(msg("actionSendNow"), "sync")));
  } else if (sync.lastDrained) {
    rows.push(syncRow("success", "check", msg("syncDone")));
  }
  if (summary.problems) {
    rows.push(syncRow("error", "stop", plural("syncProblems", summary.problems), actionLink(msg("actionReview"), "#/conflicts")));
  }
  for (const [org, count] of summary.otherOrgs) {
    rows.push(syncRow("warning", "info", msg("syncOtherOrg").replace("{org}", org).replace("{n}", String(count))));
  }
  if (summary.otherPeople) {
    rows.push(
      syncRow(
        "warning",
        "user",
        msg("syncOtherPeople").replace("{n}", String(summary.otherPeople)),
        actionButton(msg("actionDiscard"), "discard-others"),
      ),
    );
  }
  container.replaceChildren(...rows);
  const tones = rows.map((row) => row.className.split("--").pop());
  container.parentElement.dataset.tone = rows.length ? tones[0] : "none";
}

// Al cambiar de pantalla el foco va al título (lectores de pantalla y teclado).
export function focusHeading(view) {
  const heading = view.querySelector("h1");
  if (heading) heading.focus({ preventScroll: false });
}
