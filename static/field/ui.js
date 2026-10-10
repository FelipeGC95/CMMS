// Pantallas de la vista de terreno. Todo se arma clonando <template> de field/shell.html y
// escribiendo con textContent: nunca se interpreta HTML (los datos vienen del servidor o del
// teléfono y podrían traer marcas).

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

function label(kind, value) {
  const node = labels.querySelector(`[data-${kind}="${CSS.escape(String(value))}"]`);
  return node ? node.textContent : String(value);
}

export function icon(name) {
  const tpl = document.getElementById(`icon-${name}`);
  return tpl.content.firstElementChild.cloneNode(true);
}

function clone(id) {
  return document.getElementById(id).content.cloneNode(true);
}

function slot(root, name) {
  return root.querySelector(`[data-slot="${name}"]`);
}

function badge(tone, iconName, textValue) {
  const span = document.createElement("span");
  span.className = `badge badge--${tone}`;
  span.append(icon(iconName), document.createTextNode(textValue));
  return span;
}

function statusBadge(status, bootstrap) {
  const tone = STATUS_TONES[status] || "neutral";
  const name = (bootstrap.status_labels && bootstrap.status_labels[status]) || status;
  return badge(tone, TONE_ICONS[tone], name);
}

function priorityBadge(priority) {
  const tone = PRIORITY_TONES[priority];
  return tone ? badge(tone, "alert", label("priority", priority)) : null;
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
  return new Intl.DateTimeFormat("es-CL", { timeZone: "UTC", ...options }).format(
    new Date(`${isoDate}T12:00:00Z`),
  );
}

function formatDayTitle(isoDate) {
  const value = formatDay(isoDate, { weekday: "long", day: "numeric", month: "long" });
  return value.charAt(0).toUpperCase() + value.slice(1);
}

function formatShortDay(isoDate) {
  return formatDay(isoDate, { day: "numeric", month: "short" });
}

function formatDateTime(iso, timeZone) {
  return new Intl.DateTimeFormat("es-CL", { timeZone, dateStyle: "medium", timeStyle: "short" }).format(
    new Date(iso),
  );
}

function formatNumber(value) {
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

function workOrderItem(wo, bootstrap) {
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
  badges.append(statusBadge(wo.status, bootstrap));
  const priority = priorityBadge(wo.priority);
  if (priority) badges.append(priority);
  if (wo.assigned_to_me) badges.append(badge("info", "user", msg("assignedToMe")));
  return fragment;
}

export function renderList(view, { session, workOrders }) {
  const { bootstrap } = session;
  const timeZone = bootstrap.organization.timezone;
  const today = todayIn(timeZone);
  const fragment = clone("tpl-list");
  slot(fragment, "title").textContent = msg("listTitle");
  slot(fragment, "summary").textContent =
    `${workOrders.length} ${msg("workOrders")} · ${msg("syncedAt")} ${formatDateTime(session.synced_at, timeZone)}`;
  if (session.truncated) {
    const warning = slot(fragment, "truncated");
    warning.textContent = msg("truncated");
    warning.hidden = false;
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
    items.forEach((wo) => list.append(workOrderItem(wo, bootstrap)));
    days.append(day);
  }
  view.replaceChildren(fragment);
}

// ---------- Detalle de OT (solo lectura en esta etapa) ----------

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

function itemHint(item) {
  const parts = [];
  if (item.value_number !== null && item.value_number !== undefined && item.value_number !== "") {
    parts.push(`${formatNumber(item.value_number)}${item.unit ? ` ${item.unit}` : ""}`);
  }
  if (item.value_text) parts.push(item.value_text);
  if (item.min_value !== null || item.max_value !== null) {
    const low = item.min_value === null ? "…" : formatNumber(item.min_value);
    const high = item.max_value === null ? "…" : formatNumber(item.max_value);
    parts.push(`${msg("range")}: ${low} – ${high}${item.unit ? ` ${item.unit}` : ""}`);
  }
  if (item.photo_count) parts.push(`${msg("photos")}: ${item.photo_count}`);
  return parts.join(" · ");
}

function checklistItem(item, index) {
  const fragment = clone("tpl-check-item");
  const li = fragment.firstElementChild;
  if (item.result) li.classList.add(`check-item--${item.result}`);
  slot(fragment, "num").textContent = `${index}.`;
  slot(fragment, "text").textContent = item.text;
  slot(fragment, "badges").append(itemBadge(item));
  const hint = itemHint(item);
  if (hint) {
    const node = slot(fragment, "hint");
    node.textContent = hint;
    node.hidden = false;
  }
  if (item.note) {
    const note = slot(fragment, "note");
    note.textContent = item.note;
    note.hidden = false;
  }
  return fragment;
}

export function renderDetail(view, { session, workOrder: wo }) {
  const { bootstrap } = session;
  const fragment = clone("tpl-detail");
  slot(fragment, "code").textContent = wo.code;
  slot(fragment, "title").textContent = wo.title;
  const badges = slot(fragment, "badges");
  badges.append(statusBadge(wo.status, bootstrap));
  const priority = priorityBadge(wo.priority);
  if (priority) badges.append(priority);
  if (wo.assigned_to_me) badges.append(badge("info", "user", msg("assignedToMe")));

  const facts = slot(fragment, "facts");
  addFact(facts, msg("target"), targetText(wo), "box");
  addFact(facts, msg("scheduledFor"), wo.scheduled_for && formatDayTitle(wo.scheduled_for), "calendar");
  addFact(facts, msg("dueOn"), wo.due_on && formatDayTitle(wo.due_on), "calendar");
  addFact(facts, msg("assignee"), wo.assigned_to && wo.assigned_to.name, "user");
  addFact(facts, msg("estimate"), wo.estimated_minutes && `${wo.estimated_minutes} ${msg("minutes")}`, "clock");
  if (wo.meter) {
    const required = wo.meter_required ? ` · ${msg("meterRequired")}` : "";
    addFact(facts, msg("meter"), `${wo.meter.name} (${wo.meter.unit})${required}`, "clock");
  }
  if (wo.description) {
    const description = slot(fragment, "description");
    description.textContent = wo.description;
    description.hidden = false;
  }

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
    list.append(checklistItem(item, index + 1));
  });
  if (items.length) {
    const answered = items.filter((item) => item.result).length;
    slot(fragment, "progress").textContent = `${answered} ${msg("of")} ${items.length} ${msg("itemsDone")}`;
  } else {
    slot(fragment, "checklist-empty").hidden = false;
  }
  view.replaceChildren(fragment);
}

export function renderTemplate(view, id) {
  view.replaceChildren(clone(id));
}

export function renderConnection(node, online) {
  node.replaceChildren(icon(online ? "check" : "x"), document.createTextNode(msg(online ? "online" : "offline")));
  node.dataset.online = online ? "true" : "false";
}

// Al cambiar de pantalla el foco va al título (lectores de pantalla y teclado).
export function focusHeading(view) {
  const heading = view.querySelector("h1");
  if (heading) heading.focus({ preventScroll: false });
}
