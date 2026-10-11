// Repuestos en la ejecución de una OT (INV-13): previstos con "Usar" y "Agregar repuesto"
// (catálogo descargado en `reference` o texto libre). Se encolan como cualquier cambio de la
// OT ("part", con client_uuid y used_at), antes de "Completar". Nunca hay conflicto por stock:
// el servidor acepta stock negativo. Igual que ui.js: clona <template> y usa textContent.

import { canAnswer, editableDecimal, normalizeDecimal, plannedPending } from "./local.js";
import { badge, clone, formatNumber, msg, slot } from "./ui.js";

const OPTION_LIMIT = 300;

export function partRows(reference) {
  return ((reference && reference.parts) || []).map((part) => [part.id, `${part.code} · ${part.name}`]);
}

function catalogOf(reference) {
  return new Map(((reference && reference.parts) || []).map((part) => [part.id, part]));
}

function quantityText(value, unit) {
  return `${formatNumber(value)}${unit ? ` ${unit}` : ""}`;
}

function option(value, textValue, selected = false) {
  const node = document.createElement("option");
  node.value = value;
  node.textContent = textValue;
  node.selected = selected;
  return node;
}

// Opciones del buscador: las que coinciden (hasta el tope), más la elegida.
export function fillPartOptions(select, rows, query = "", selected = select.value) {
  const needle = query.trim().toLowerCase();
  const matches = (needle ? rows.filter(([, text]) => text.toLowerCase().includes(needle)) : rows).slice(0, OPTION_LIMIT);
  if (selected && !matches.some(([value]) => value === selected)) {
    const kept = rows.find(([value]) => value === selected);
    if (kept) matches.unshift(kept);
  }
  select.replaceChildren(option("", msg("choose")), ...matches.map(([value, text]) => option(value, text, value === selected)));
}

function plannedItem(planned, editable) {
  const fragment = clone("tpl-planned-part");
  const li = fragment.firstElementChild;
  li.dataset.part = planned.part;
  slot(fragment, "code").textContent = planned.code;
  slot(fragment, "name").textContent = planned.name;
  const pending = plannedPending(planned);
  slot(fragment, "pending").textContent = `${msg("partPending")}: ${quantityText(pending, planned.unit)}`;
  const input = slot(fragment, "qty");
  const id = `part-qty-${planned.id}`;
  input.id = id;
  slot(fragment, "qty-label").htmlFor = id;
  input.value = editableDecimal(pending);
  slot(fragment, "unit").textContent = planned.unit || "";
  slot(fragment, "use").hidden = !editable;
  return fragment;
}

function usedItem(line, catalog) {
  const fragment = clone("tpl-used-part");
  const part = line.part ? catalog.get(line.part) : null;
  const unit = line.unit || (part && part.unit) || "";
  slot(fragment, "text").textContent = `${line.description} · ${quantityText(line.quantity, unit)}`;
  if (line._pending) slot(fragment, "badges").append(badge("warning", "clock", msg("badgePending")));
  return fragment;
}

// Formulario "Agregar repuesto": catálogo (si se descargó) o texto libre.
export function setupForm(form, reference, values = {}) {
  const rows = partRows(reference);
  const hasCatalog = rows.length > 0;
  const free = !hasCatalog || Boolean(values.description && !values.part);
  fillPartOptions(form.elements.part, rows, "", values.part || "");
  form.elements.free.checked = free;
  slot(form, "free-toggle").hidden = !hasCatalog;
  form.elements.quantity.value = editableDecimal(values.quantity);
  form.elements.description.value = values.part ? "" : values.description || "";
  form.elements.unit.value = values.part ? "" : values.unit || "";
  showMode(form, reference);
}

export function showMode(form, reference) {
  const free = form.elements.free.checked;
  form.querySelectorAll("[data-mode]").forEach((node) => {
    node.hidden = node.dataset.mode !== (free ? "free" : "catalog");
  });
  const part = catalogOf(reference).get(form.elements.part.value);
  slot(form, "part-unit").textContent = !free && part && part.unit ? `(${part.unit})` : "";
}

// Lee el formulario; el payload es lo que se envía (más client_uuid) a `parts/`.
export function readForm(form, reference) {
  const errors = {};
  const quantity = normalizeDecimal(form.elements.quantity.value);
  if (quantity === null || Number(quantity) <= 0) errors.quantity = [msg("invalidQuantity")];
  const payload = { quantity };
  if (form.elements.free.checked) {
    const description = form.elements.description.value.trim();
    if (!description) errors.description = [msg("requiredPartDescription")];
    payload.description = description;
    payload.unit = form.elements.unit.value.trim();
  } else {
    const part = catalogOf(reference).get(form.elements.part.value);
    if (!part) errors.part = [msg("choosePart")];
    else Object.assign(payload, { part: part.id, description: `${part.code} · ${part.name}`, unit: part.unit });
  }
  return { payload, errors };
}

// "Usar" un previsto: la cantidad escrita (por defecto, lo que falta).
export function readUse(li, wo) {
  const planned = (wo.planned_parts || []).find((row) => row.part === li.dataset.part);
  const quantity = normalizeDecimal(slot(li, "qty").value);
  if (!planned) return { error: msg("choosePart") };
  if (quantity === null || Number(quantity) <= 0) return { error: msg("invalidQuantity") };
  return {
    payload: { part: planned.part, description: `${planned.code} · ${planned.name}`, unit: planned.unit, quantity },
  };
}

// Sección "Repuestos" del detalle de la OT (el <section data-slot="parts"> de tpl-detail).
export function renderParts(root, { wo, bootstrap, reference }) {
  const section = slot(root, "parts");
  if (!section) return;
  const editable = canAnswer(wo, bootstrap);
  const catalog = catalogOf(reference);
  const planned = (wo.planned_parts || []).filter((row) => plannedPending(row) > 0);
  const used = wo.parts || [];
  const plannedList = slot(section, "planned");
  plannedList.replaceChildren(...planned.map((row) => plannedItem(row, editable)));
  slot(section, "planned-block").hidden = planned.length === 0;
  const usedList = slot(section, "used");
  usedList.replaceChildren(...used.map((line) => usedItem(line, catalog)));
  slot(section, "used-block").hidden = used.length === 0;
  slot(section, "parts-empty").hidden = planned.length > 0 || used.length > 0;
  slot(section, "part-add-open").hidden = !editable;
  const form = mountForm(slot(section, "part-form"));
  form.hidden = true;
  setupForm(form, reference);
  section.hidden = !editable && planned.length === 0 && used.length === 0;
}

// Pantalla "Corregir y reenviar" de un repuesto rechazado (400).
export function renderFix(view, { wo, reference, entry }) {
  const fragment = clone("tpl-part-fix");
  slot(fragment, "back").href = `#/wo/${encodeURIComponent(wo.id)}`;
  slot(fragment, "code").textContent = wo.code;
  slot(fragment, "title").textContent = wo.title;
  const form = mountForm(slot(fragment, "part-form"));
  slot(form, "cancel").hidden = true;
  setupForm(form, reference, entry.payload);
  view.replaceChildren(fragment);
}

// El formulario es uno solo (tpl-part-form): en el detalle de la OT y al corregir.
function mountForm(host) {
  host.replaceChildren(clone("tpl-part-form"));
  return host.querySelector("form[data-form='part']");
}
