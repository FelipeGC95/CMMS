// Envío de la cola a /field/api/ (ADR-011 §1e). Corre en la página, nunca en el Service Worker.
//   Disparadores: al abrir, al volver la conexión (`online`), al volver a la pestaña
//   (`visibilitychange`), tras encolar y cada 60 s con la pestaña visible.
//   Un envío a la vez: FIFO por OT; lecturas y solicitudes son independientes entre sí.
//   2xx → hecho · 401 / mfa_required → pausa todo · 409 / 404 → conflicto (bloquea lo que
//   sigue de esa OT) · 400 → rechazado, se corrige y reenvía con el mismo client_uuid ·
//   red / 5xx → reintento con espera exponencial.

import * as api from "./api.js";
import * as db from "./db.js";
import { owns } from "./queue.js";

const PERIOD_MS = 60 * 1000;
const RETRY_BASE_MS = 5 * 1000;
const RETRY_MAX_MS = 5 * 60 * 1000;
const LOCK_NAME = "galbust-field-sync";

const state = {
  identity: null,
  pause: null, // null | "auth" | "mfa" | "upgrade" | "identity"
  running: false,
  sentThisRun: 0,
  lastDrained: false,
};
let listeners = { onChange: () => {}, onSent: () => {}, onDrained: () => {}, onIdentityChanged: () => {} };
let retryTimer = null;

export function status() {
  return { pause: state.pause, running: state.running, lastDrained: state.lastDrained };
}

export function configure(handlers) {
  listeners = { ...listeners, ...handlers };
}

// Solo se envía con una identidad confirmada por `bootstrap` en esta carga de la página.
export function setIdentity(identity) {
  state.identity = identity;
}

export function setPause(reason) {
  state.pause = reason;
  listeners.onChange();
}

function backoff(attempts) {
  const delay = Math.min(RETRY_MAX_MS, RETRY_BASE_MS * 2 ** Math.max(0, attempts - 1));
  return delay / 2 + Math.random() * (delay / 2);
}

// Siguiente cambio a enviar: el primero de cada OT (o cambio suelto) en orden de llegada.
export function nextEntry(entries, identity, now = Date.now()) {
  const blocked = new Set();
  for (const entry of entries) {
    if (!owns(entry, identity)) continue;
    const lane = entry.wo_id || entry.client_uuid;
    if (blocked.has(lane)) continue;
    blocked.add(lane); // lo que sigue de esta OT espera a este cambio
    if (entry.status === "conflict" || entry.status === "rejected") continue;
    if (entry.next_at && entry.next_at > now) continue;
    return entry;
  }
  return null;
}

async function formWithBlob(entry, fields, fileField) {
  const form = new FormData();
  Object.entries(fields).forEach(([key, value]) => {
    if (value !== null && value !== undefined && value !== "") form.append(key, String(value));
  });
  const blobId = (entry.blob_ids || [])[0];
  if (blobId) {
    const record = await db.getBlob(blobId);
    if (record) form.append(fileField, record.blob, record.name || "foto.jpg");
  }
  return form;
}

async function send(entry) {
  const wo = entry.wo_id ? `work-orders/${entry.wo_id}/` : "";
  const item = entry.item_id ? `checklist-items/${entry.item_id}/` : "";
  const { payload } = entry;
  const org = entry.organization_id; // el servidor rechaza el envío si la sesión es de otra organización
  switch (entry.kind) {
    case "transition":
      return api.postJson(`${wo}transition/`, { ...payload, client_uuid: entry.client_uuid }, org);
    case "answer":
      return api.postJson(`${wo}${item}answer/`, payload, org);
    case "item_photo":
      return api.postForm(
        `${wo}${item}photos/`,
        await formWithBlob(entry, { client_uuid: entry.client_uuid, taken_at: payload.taken_at }, "file"),
        org,
      );
    case "wo_photo":
      return api.postForm(
        `${wo}photos/`,
        await formWithBlob(entry, { client_uuid: entry.client_uuid, taken_at: payload.taken_at }, "file"),
        org,
      );
    case "complete":
      return api.postJson(`${wo}complete/`, { ...payload, client_uuid: entry.client_uuid }, org);
    case "labor":
      return api.postJson(`${wo}labor/`, { ...payload, client_uuid: entry.client_uuid }, org);
    case "part":
      return api.postJson(`${wo}parts/`, { ...payload, client_uuid: entry.client_uuid }, org);
    case "finding":
      // Va detrás de la respuesta del ítem y antes de "Completar": la cola es FIFO por OT.
      return api.postJson(`${wo}checklist/${entry.item_id}/finding/`, { ...payload, client_uuid: entry.client_uuid }, org);
    case "reading":
      return api.postJson("readings/", { ...payload, client_uuid: entry.client_uuid }, org);
    case "request":
      if ((entry.blob_ids || []).length) {
        return api.postForm("requests/", await formWithBlob(entry, { ...payload, client_uuid: entry.client_uuid }, "photo"), org);
      }
      return api.postJson("requests/", { ...payload, client_uuid: entry.client_uuid }, org);
    default:
      throw new Error(`unknown kind ${entry.kind}`);
  }
}

async function drain() {
  const identity = state.identity;
  for (;;) {
    if (state.pause || !identity) return;
    const entry = nextEntry(await db.listQueue(), identity);
    if (!entry) return;
    await db.putEntry({ ...entry, status: "sending" });
    listeners.onChange();
    try {
      const result = await send(entry);
      await db.deleteEntry(entry);
      state.sentThisRun += 1;
      await listeners.onSent(entry, result.data);
    } catch (error) {
      if (!(error instanceof api.ApiError)) {
        // Error del propio teléfono (p. ej. la foto ya no existe): no se reintenta en bucle.
        await db.putEntry({ ...entry, status: "rejected", error: { code: "client", message: String(error.message), details: {} } });
        continue;
      }
      const kind = error.kind;
      const failure = { code: error.code, message: error.message, details: error.details, status: error.status };
      if (kind === "retry") {
        const attempts = (entry.attempts || 0) + 1;
        await db.putEntry({ ...entry, status: "pending", attempts, next_at: Date.now() + backoff(attempts), error: failure });
        if (error.status === 0) return; // sin red: lo demás tampoco saldría
        continue;
      }
      await db.putEntry({ ...entry, status: kind === "rejected" ? "rejected" : kind === "conflict" ? "conflict" : "pending", error: failure });
      if (kind === "auth" || kind === "mfa" || kind === "upgrade" || kind === "identity") {
        state.pause = kind;
        if (kind === "identity") listeners.onIdentityChanged();
        return;
      }
    } finally {
      listeners.onChange();
    }
  }
}

function scheduleRetry(entries) {
  clearTimeout(retryTimer);
  const waits = entries
    .filter((entry) => owns(entry, state.identity) && entry.status === "pending" && entry.next_at)
    .map((entry) => entry.next_at - Date.now());
  if (waits.length) retryTimer = setTimeout(run, Math.max(1000, Math.min(...waits)));
}

async function runLocked() {
  if (state.running) return;
  state.running = true;
  state.sentThisRun = 0;
  listeners.onChange();
  try {
    await recover();
    await drain();
  } finally {
    state.running = false;
    const entries = await db.listQueue();
    state.lastDrained = state.sentThisRun > 0 && !entries.some((entry) => owns(entry, state.identity));
    scheduleRetry(entries);
    listeners.onChange();
    if (state.sentThisRun > 0) listeners.onDrained(state.sentThisRun);
  }
}

// Envía lo pendiente. Con varias pestañas abiertas, solo una envía a la vez (Web Locks).
export async function run() {
  if (!state.identity || state.pause || !navigator.onLine) {
    listeners.onChange();
    return;
  }
  if (navigator.locks && navigator.locks.request) {
    await navigator.locks.request(LOCK_NAME, { ifAvailable: true }, async (lock) => {
      if (lock) await runLocked();
    });
  } else {
    await runLocked();
  }
}

// Los envíos que quedaron "enviando" (pestaña cerrada a medias) vuelven a pendientes:
// el client_uuid hace que el reenvío no duplique. Corre con el candado tomado.
async function recover() {
  for (const entry of await db.listQueue()) {
    if (entry.status === "sending") await db.putEntry({ ...entry, status: "pending" });
  }
}

export function isRunning() {
  return state.running;
}

export function start() {
  window.addEventListener("online", () => run());
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") run();
  });
  setInterval(() => {
    if (document.visibilityState === "visible") run();
  }, PERIOD_MS);
}
