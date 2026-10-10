// Copia local de las OT: aplica a los datos descargados los cambios que aún esperan en la cola,
// para que la persona vea lo que hizo sin conexión. Las reglas imitan las del servidor
// (apps/work/services.py); si difieren, al volver a descargar gana la respuesta del servidor.

export const LOCKED_STATUSES = new Set(["completed", "closed", "cancelled"]);
// Las horas se registran hasta que la OT se cierra o se cancela (COST_LOCKED_STATUSES del servidor).
export const LABOR_LOCKED_STATUSES = new Set(["closed", "cancelled"]);
// Cambios que la persona aún quiere enviar (los en conflicto ya no se aplican).
export const LIVE_STATUSES = new Set(["pending", "sending", "rejected"]);
const ACTIONS = ["start", "resume", "pause", "complete"];

export function transitionsFor(status, bootstrap) {
  const rules = bootstrap.execution_transitions || {};
  return [...new Set(ACTIONS.filter((name) => rules[name] && rules[name].from.includes(status)).map((name) => rules[name].to))];
}

// Acciones de ejecución que se ofrecen: las permitidas por el servidor (o, tras un cambio
// local, por las reglas de `bootstrap`).
export function executionActions(wo, bootstrap) {
  if (!bootstrap.permissions.execute) return [];
  const rules = bootstrap.execution_transitions || {};
  const allowed = new Set(wo.allowed_transitions || []);
  return ACTIONS.filter((name) => rules[name] && rules[name].from.includes(wo.status) && allowed.has(rules[name].to));
}

export function canAnswer(wo, bootstrap) {
  return Boolean(bootstrap.permissions.execute) && !LOCKED_STATUSES.has(wo.status);
}

function decimal(value) {
  const text = normalizeDecimal(value);
  return text === null ? null : Number(text);
}

// "6,5" → "6.5" y "12.480,5" → "12480.5" (la API recibe decimales como texto con punto).
export function normalizeDecimal(value) {
  let text = String(value === null || value === undefined ? "" : value).trim().replace(/\s/g, "");
  if (text.includes(",")) text = text.replace(/\./g, "").replace(",", ".");
  return text && Number.isFinite(Number(text)) ? text : null;
}

// "6.500" → "6,5" para editarlo en un campo (sin separador de miles).
export function editableDecimal(value) {
  if (value === null || value === undefined || value === "") return "";
  return String(Number(value)).replace(".", ",");
}

export function measurementResult(item, value) {
  const number = decimal(value);
  const low = decimal(item.min_value);
  const high = decimal(item.max_value);
  if (number === null) return "";
  return (low !== null && number < low) || (high !== null && number > high) ? "not_ok" : "ok";
}

function startIfNeeded(wo, bootstrap) {
  const start = (bootstrap.execution_transitions || {}).start;
  if (start && start.from.includes(wo.status)) {
    wo.status = start.to;
    wo.allowed_transitions = transitionsFor(wo.status, bootstrap);
  }
}

function applyAnswer(item, body) {
  if (item.item_type === "measurement" && body.value_number !== undefined) {
    item.value_number = body.value_number;
    item.result = measurementResult(item, body.value_number);
  } else if (item.item_type === "text" && body.value_text !== undefined) {
    item.value_text = body.value_text;
    item.result = "done";
  } else if (item.item_type === "signature" && body.signature) {
    item.result = "done";
    item._signature = body.signature;
  } else if (body.result) {
    item.result = body.result;
  }
  if (body.note !== undefined) item.note = body.note;
  item.answered_at = body.answered_at;
}

// Aplica un cambio de la cola a una OT (la modifica y la devuelve).
export function applyEntry(wo, entry, bootstrap) {
  const item = entry.item_id ? (wo.checklist || []).find((row) => row.id === entry.item_id) : null;
  const { payload } = entry;
  switch (entry.kind) {
    case "transition":
      wo.status = payload.to_status;
      wo.allowed_transitions = transitionsFor(wo.status, bootstrap);
      break;
    case "answer":
      if (item) {
        applyAnswer(item, payload);
        startIfNeeded(wo, bootstrap);
      }
      break;
    case "item_photo":
      if (item) {
        item.photo_count = (item.photo_count || 0) + 1;
        if (item.item_type === "photo") item.result = "done";
        startIfNeeded(wo, bootstrap);
      }
      break;
    case "wo_photo":
      wo._photo_count = (wo._photo_count || 0) + 1;
      break;
    case "complete":
      wo.status = "completed";
      wo.allowed_transitions = [];
      break;
    default:
      break;
  }
  return wo;
}

// Minutos trabajados según los tramos locales (iniciar/reanudar → pausar/completar).
export function workedMinutes(timer, now = Date.now()) {
  const ms = (timer.spans || []).reduce(
    (total, span) => total + Math.max(0, (span.end ? Date.parse(span.end) : now) - Date.parse(span.start)),
    0,
  );
  return Math.round(ms / 60000);
}

export function updateTimer(timer, toStatus, at) {
  const spans = timer.spans || [];
  const open = spans.length && !spans[spans.length - 1].end;
  if (toStatus === "in_progress" && !open) spans.push({ start: at, end: null });
  if (toStatus !== "in_progress" && open) spans[spans.length - 1].end = at;
  timer.spans = spans;
  return timer;
}
