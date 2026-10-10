// Formularios de terreno: completar OT, nueva solicitud, lectura de medidor y la lista de
// cambios con conflicto o rechazados. Solo arman la pantalla y leen los valores; app.js
// encola. Igual que ui.js: clonan <template> y escriben con textContent.

import { editableDecimal, normalizeDecimal, workedMinutes } from "./local.js";
import { badge, choices, clone, formatDateTime, formatNumber, icon, msg, setFlash, slot } from "./ui.js";

const OPTION_LIMIT = 300;

function option(value, textValue, selected = false) {
  const node = document.createElement("option");
  node.value = value;
  node.textContent = textValue;
  node.selected = selected;
  return node;
}

function fillSelect(select, rows, selected, { blank = true } = {}) {
  const options = blank ? [option("", msg("choose"))] : [];
  for (const [value, textValue] of rows) options.push(option(value, textValue, value === selected));
  select.replaceChildren(...options);
}

// "2026-09-29T11:10:00Z" ↔ valor de <input type="datetime-local"> (hora del teléfono).
function toLocalInput(iso) {
  if (!iso) return "";
  const date = new Date(iso);
  const pad = (n) => String(n).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

function fromLocalInput(value) {
  return value ? new Date(value).toISOString() : null;
}

// Errores por campo que devolvió el servidor (400) o la validación local.
export function showErrors(root, errors) {
  root.querySelectorAll("[data-error]").forEach((node) => {
    node.hidden = true;
    node.replaceChildren();
  });
  const general = [];
  for (const [field, messages] of Object.entries(errors || {})) {
    const list = Array.isArray(messages) ? messages : [String(messages)];
    const target = root.querySelector(`[data-field="${CSS.escape(field)}"] [data-error]`);
    if (target) {
      target.replaceChildren(icon("stop"), document.createTextNode(list.join(" ")));
      target.hidden = false;
    } else {
      general.push(...list);
    }
  }
  const box = slot(root, "errors");
  if (box) {
    slot(root, "errors-text").textContent = general.join(" ");
    box.hidden = general.length === 0;
  }
  const first = root.querySelector("[data-error]:not([hidden]), [data-slot='errors']:not([hidden])");
  if (first) first.scrollIntoView({ block: "center" });
}

// ---------- Completar OT ----------

export function renderComplete(view, { session, wo, timer, reference, entry }) {
  const { bootstrap } = session;
  const fragment = clone("tpl-complete");
  const back = slot(fragment, "back");
  back.href = `#/wo/${encodeURIComponent(wo.id)}`;
  slot(fragment, "code").textContent = wo.code;
  slot(fragment, "title").textContent = wo.title;
  const form = fragment.querySelector("form");
  const values = entry ? entry.payload : {};
  const minutes = workedMinutes(timer);
  form.elements.labor_minutes.value = values.labor_minutes ?? (minutes > 0 ? minutes : "");
  slot(fragment, "labor-help").hidden = !(minutes > 0);
  form.elements.notes.value = values.notes || "";
  if (wo.meter) {
    slot(fragment, "meter").hidden = false;
    slot(fragment, "meter-name").textContent = wo.meter.name ? `· ${wo.meter.name} (${wo.meter.unit})` : `(${wo.meter.unit})`;
    const input = form.elements.meter_value;
    input.required = Boolean(wo.meter_required);
    input.value = editableDecimal(values.meter_value);
    slot(fragment, wo.meter_required ? "meter-optional" : "meter-required").hidden = true;
    const meter = reference && reference.meters.find((row) => row.id === wo.meter.id);
    if (meter && meter.current_value !== null) input.placeholder = formatNumber(meter.current_value);
  }
  const withFailure = wo.work_type === "corrective" && Boolean(wo.asset);
  if (withFailure) {
    slot(fragment, "failure").hidden = false;
    slot(fragment, "notes-label").hidden = true;
    slot(fragment, "notes-label-failure").hidden = false;
    fillSelect(form.elements.impact, choices("impact"), values.impact || "");
    form.elements.impact.required = true;
    form.elements.symptom.value = values.symptom || "";
    form.elements.failure_started_at.value = toLocalInput(values.failure_started_at);
    form.elements.failure_ended_at.value = toLocalInput(values.failure_ended_at);
    const codes = (reference && reference.failure_codes) || {};
    const required = Boolean(bootstrap.organization.require_failure_details);
    form.querySelectorAll("[data-codes]").forEach((select) => {
      const rows = (codes[select.dataset.codes] || []).map((code) => [code.id, `${code.code} · ${code.name}`]);
      fillSelect(select, rows, values[select.name] || "");
      select.required = required;
    });
  }
  if (values.signature) form.elements.signature.value = values.signature;
  view.replaceChildren(fragment);
  if (entry && entry.error) showErrors(view, entry.error.details || { non_field_errors: [entry.error.message] });
}

// Valida lo que el teléfono puede saber; el resto lo valida el servidor (400 → "Corregir").
export function readComplete(form, wo) {
  const errors = {};
  const get = (name) => (form.elements[name] ? form.elements[name].value.trim() : "");
  const minutes = Number(get("labor_minutes"));
  if (get("labor_minutes") === "" || !Number.isInteger(minutes) || minutes < 0 || minutes > 1440) {
    errors.labor_minutes = [form.elements.labor_minutes.validationMessage || msg("invalidNumber")];
  }
  const payload = { labor_minutes: minutes, notes: get("notes") };
  if (wo.meter) {
    const raw = get("meter_value");
    const value = normalizeDecimal(raw);
    if (raw && value === null) errors.meter_value = [msg("invalidNumber")];
    else if (!raw && wo.meter_required) errors.meter_value = [form.elements.meter_value.validationMessage || msg("invalidNumber")];
    if (value !== null) payload.meter_value = value;
  }
  if (wo.work_type === "corrective" && wo.asset) {
    payload.symptom = get("symptom");
    payload.impact = get("impact");
    if (!payload.impact) errors.impact = [form.elements.impact.validationMessage || msg("choose")];
    payload.failure_started_at = fromLocalInput(get("failure_started_at"));
    payload.failure_ended_at = fromLocalInput(get("failure_ended_at"));
    for (const name of ["failure_mode", "failure_cause"]) {
      const value = get(name);
      if (form.elements[name].required && !value) errors[name] = [form.elements[name].validationMessage || msg("choose")];
      payload[name] = value || null;
    }
  }
  const signature = get("signature");
  if (signature) payload.signature = signature;
  const missing = (wo.checklist || []).filter((item) => item.required && !item.result).length;
  if (missing) errors.non_field_errors = [`${msg("missingRequired")}: ${missing}.`];
  return { payload, errors };
}

// ---------- Nueva solicitud y lectura de medidor ----------

function assetText(asset) {
  return asset.location_name ? `${asset.code} · ${asset.name} — ${asset.location_name}` : `${asset.code} · ${asset.name}`;
}

function filterOptions(rows, query) {
  const needle = query.trim().toLowerCase();
  const matches = needle ? rows.filter(([, textValue]) => textValue.toLowerCase().includes(needle)) : rows;
  return matches.slice(0, OPTION_LIMIT);
}

// Las primeras opciones, más la elegida si quedó fuera del tope.
function withSelected(rows, selected) {
  const shown = filterOptions(rows, "");
  if (selected && !shown.some(([value]) => value === selected)) shown.unshift(...rows.filter(([value]) => value === selected));
  return shown;
}

// Busca dentro de un <select>: rehace sus opciones con las que coinciden.
export function applyFilter(input, rows) {
  const select = document.getElementById(input.dataset.filter);
  const current = select.value;
  const matches = filterOptions(rows, input.value);
  if (current && !matches.some(([value]) => value === current)) {
    const kept = rows.find(([value]) => value === current);
    if (kept) matches.unshift(kept);
  }
  fillSelect(select, matches, current);
}

export function assetRows(reference) {
  return reference ? reference.assets.map((asset) => [asset.id, assetText(asset)]) : [];
}

export function locationRows(reference) {
  const seen = new Map();
  for (const asset of (reference && reference.assets) || []) {
    if (asset.location_id && !seen.has(asset.location_id)) seen.set(asset.location_id, asset.location_name || asset.location_code);
  }
  return [...seen.entries()].sort((a, b) => a[1].localeCompare(b[1], "es"));
}

export function meterRows(reference) {
  if (!reference) return [];
  const assets = new Map(reference.assets.map((asset) => [asset.id, asset]));
  return reference.meters.map((meter) => {
    const asset = assets.get(meter.asset_id);
    const owner = asset ? `${asset.code} · ${asset.name} — ` : "";
    return [meter.id, `${owner}${meter.name} (${meter.unit})`];
  });
}

export function renderRequest(view, { reference, entry, params }) {
  const fragment = clone("tpl-request");
  const form = fragment.querySelector("form");
  const values = entry ? entry.payload : {};
  const assetId = values.asset || params.get("asset") || "";
  fillSelect(form.elements.asset, withSelected(assetRows(reference), assetId), assetId);
  fillSelect(form.elements.location, locationRows(reference), values.location || "");
  form.elements.asset_stopped.checked = Boolean(values.asset_stopped);
  form.elements.description.value = values.description || "";
  if (!reference) {
    const warning = slot(fragment, "no-reference");
    warning.textContent = msg("noReference");
    warning.hidden = false;
  }
  view.replaceChildren(fragment);
  if (entry && entry.error) showErrors(view, entry.error.details || { non_field_errors: [entry.error.message] });
}

export function readRequest(form) {
  const errors = {};
  const asset = form.elements.asset.value;
  const location = form.elements.location.value;
  const description = form.elements.description.value.trim();
  if (!description) errors.description = [form.elements.description.validationMessage || msg("requiredText")];
  if (!asset && !location) errors.asset = [msg("chooseTarget")];
  const payload = { description, asset_stopped: Boolean(asset) && form.elements.asset_stopped.checked };
  if (asset) payload.asset = asset;
  else if (location) payload.location = location;
  return { payload, errors };
}

export function renderReading(view, { reference, entry, params }) {
  const fragment = clone("tpl-reading");
  const form = fragment.querySelector("form");
  const values = entry ? entry.payload : {};
  const meterId = values.meter || params.get("meter") || "";
  fillSelect(form.elements.meter, withSelected(meterRows(reference), meterId), meterId);
  form.elements.value.value = editableDecimal(values.value);
  form.elements.is_reset.checked = Boolean(values.is_reset);
  if (!reference) {
    const warning = slot(fragment, "no-reference");
    warning.textContent = msg("noReference");
    warning.hidden = false;
  }
  view.replaceChildren(fragment);
  describeMeter(view, reference);
  if (entry && entry.error) showErrors(view, entry.error.details || { non_field_errors: [entry.error.message] });
}

// Unidad y lectura actual del medidor elegido.
export function describeMeter(root, reference) {
  const form = root.querySelector("form[data-form='reading']");
  if (!form) return;
  const meter = reference && reference.meters.find((row) => row.id === form.elements.meter.value);
  slot(root, "unit").textContent = meter ? `(${meter.unit})` : "";
  const current = slot(root, "current");
  current.textContent =
    meter && meter.current_value !== null ? `${msg("currentValue")}: ${formatNumber(meter.current_value)} ${meter.unit}` : "";
}

export function readReading(form) {
  const errors = {};
  const meter = form.elements.meter.value;
  const value = normalizeDecimal(form.elements.value.value);
  if (!meter) errors.meter = [msg("chooseMeter")];
  if (value === null) errors.value = [msg("invalidNumber")];
  return { payload: { meter, value, is_reset: form.elements.is_reset.checked }, errors };
}

// ---------- Conflictos o rechazados ----------

const FIXABLE = new Set(["complete", "request", "reading", "answer"]);

function detailLines(details) {
  const lines = [];
  for (const [field, messages] of Object.entries(details || {})) {
    if (field === "setup_url" || field === "min_client_version") continue;
    const list = Array.isArray(messages) ? messages : [messages];
    for (const message of list) lines.push(typeof message === "string" ? message : JSON.stringify(message));
  }
  return lines;
}

function kindKey(kind) {
  return `kind${kind
    .split("_")
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join("")}`;
}

export function fixHref(entry) {
  if (entry.kind === "answer") return `#/wo/${encodeURIComponent(entry.wo_id)}`;
  return `#/fix/${encodeURIComponent(entry.client_uuid)}`;
}

export function renderConflicts(view, { session, entries, flash }) {
  const timeZone = session ? session.bootstrap.organization.timezone : undefined;
  const fragment = clone("tpl-conflicts");
  setFlash(fragment, flash);
  const list = slot(fragment, "items");
  for (const entry of entries) {
    const card = clone("tpl-problem");
    const li = card.firstElementChild;
    li.dataset.clientUuid = entry.client_uuid;
    const rejected = entry.status === "rejected";
    slot(card, "badge").append(
      rejected ? badge("warning", "alert", msg("statusRejected")) : badge("error", "stop", msg("statusConflict")),
    );
    slot(card, "title").textContent = `${msg(kindKey(entry.kind))}${entry.label ? ` · ${entry.label}` : ""}`;
    slot(card, "meta").textContent = `${msg("madeAt")} ${formatDateTime(entry.created_at, timeZone)}`;
    slot(card, "message").textContent = (entry.error && entry.error.message) || "";
    const lines = detailLines(entry.error && entry.error.details);
    if (lines.length) {
      const details = slot(card, "details");
      for (const line of lines) {
        const item = document.createElement("li");
        item.textContent = line;
        details.append(item);
      }
      details.hidden = false;
    }
    slot(card, rejected ? "help-rejected" : "help-conflict").hidden = false;
    if (rejected && FIXABLE.has(entry.kind)) {
      const fix = slot(card, "fix");
      fix.href = fixHref(entry);
      fix.hidden = false;
    }
    list.append(card);
  }
  slot(fragment, "none").hidden = entries.length > 0;
  view.replaceChildren(fragment);
}

