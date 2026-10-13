// Cola de cambios hechos en el teléfono (ADR-011 §1e). Cada cambio guarda un client_uuid
// estable (los reintentos no duplican), la persona y la organización que lo hicieron, y se
// aplica de inmediato a la copia local de la OT. sync.js los envía de a uno.

import * as db from "./db.js";
import { LIVE_STATUSES, PROBLEM_STATUSES, applyEntry, updateTimer } from "./local.js";

export { PROBLEM_STATUSES };

// UUID v4. crypto.randomUUID solo existe en contextos seguros (https o localhost).
export function uuid4() {
  if (crypto.randomUUID) return crypto.randomUUID();
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = [...bytes].map((b) => b.toString(16).padStart(2, "0")).join("");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

export const owns = (entry, identity) =>
  Boolean(identity) && entry.user_id === identity.user_id && entry.organization_id === identity.organization_id;

// Agrega un cambio. Una respuesta del mismo ítem que aún no se envía (o que fue rechazada)
// se reemplaza en su lugar y conserva su client_uuid ("Corregir y reenviar").
export async function enqueue({ identity, session, kind, wo = null, itemId = null, payload, blobs = [], label }) {
  const entries = await db.listQueue();
  let entry =
    kind === "answer"
      ? entries.find(
          (row) =>
            row.kind === "answer" &&
            row.item_id === itemId &&
            owns(row, identity) &&
            (row.status === "pending" || row.status === "rejected"),
        )
      : null;
  const blobIds = [];
  for (const blob of blobs) {
    const id = uuid4();
    await db.putBlob({ id, ...identity, blob: blob.blob, name: blob.name, type: blob.type, created_at: new Date().toISOString() });
    blobIds.push(id);
  }
  if (entry) {
    entry = { ...entry, payload, status: "pending", error: null, attempts: 0, next_at: 0 };
  } else {
    entry = {
      client_uuid: uuid4(),
      seq: db.nextSeq(),
      kind,
      wo_id: wo ? wo.id : null,
      item_id: itemId,
      user_id: identity.user_id,
      organization_id: identity.organization_id,
      organization_name: identity.organization_name,
      status: "pending",
      attempts: 0,
      next_at: 0,
      error: null,
      payload,
      blob_ids: blobIds,
      label,
      created_at: new Date().toISOString(),
    };
  }
  await db.putEntry(entry);
  if (wo) {
    const before = wo.status;
    applyEntry(wo, entry, session.bootstrap);
    await db.putWorkOrder(wo);
    // Tramos de trabajo: iniciar o reanudar abre uno; pausar o completar lo cierra.
    // Responder el primer ítem también inicia la OT (como en el servidor).
    if (wo.status !== before || kind === "complete") {
      const at = payload.at || payload.answered_at || payload.taken_at || new Date().toISOString();
      const timer = await db.getTimer(wo.id, identity);
      await db.saveTimer(updateTimer(timer, wo.status, at));
    }
  }
  return entry;
}

// Vuelve a pedir el envío de un cambio rechazado tras corregirlo. Con el mismo client_uuid
// (un 400 no lo deja usado en el servidor); `renew` le da uno nuevo cuando el servidor dijo que
// esa clave ya se usó en otra operación (409 `conflict`). Conserva su lugar en la cola (seq).
export async function resubmit(entry, payload, { renew = false } = {}) {
  const updated = { ...entry, payload, status: "pending", error: null, attempts: 0, next_at: 0 };
  if (renew) updated.client_uuid = uuid4();
  await db.putEntry(updated);
  if (renew) await db.deleteEntry({ ...entry, blob_ids: [] }); // las fotos pasan al cambio nuevo
  return updated;
}

export async function discard(entry) {
  await db.deleteEntry(entry);
}

// Tras descargar las OT del servidor, les aplica los cambios que aún no llegan a él.
export async function reapply(identity, bootstrap) {
  const entries = (await db.listQueue()).filter(
    (entry) => owns(entry, identity) && entry.wo_id && LIVE_STATUSES.has(entry.status),
  );
  const touched = new Map();
  for (const entry of entries) {
    let wo = touched.get(entry.wo_id);
    if (wo === undefined) {
      wo = (await db.getWorkOrder(entry.wo_id, identity)) || null;
      touched.set(entry.wo_id, wo);
    }
    if (wo) applyEntry(wo, entry, bootstrap);
  }
  for (const wo of touched.values()) if (wo) await db.putWorkOrder(wo);
}

// Resumen para la barra de sincronización y las insignias de cada OT.
export function summarize(entries, identity) {
  const summary = {
    pending: 0,
    problems: 0,
    otherOrgs: new Map(),
    otherPeople: 0,
    byWorkOrder: new Map(),
    byItem: new Map(),
    findings: new Map(), // aviso de hallazgo en cola por ítem (por enviar o con problema)
    mine: [],
  };
  for (const entry of entries) {
    if (!owns(entry, identity)) {
      if (identity && entry.user_id === identity.user_id) {
        const name = entry.organization_name || "";
        summary.otherOrgs.set(name, (summary.otherOrgs.get(name) || 0) + 1);
      } else {
        summary.otherPeople += 1;
      }
      continue;
    }
    summary.mine.push(entry);
    const problem = PROBLEM_STATUSES.has(entry.status);
    if (problem) summary.problems += 1;
    else summary.pending += 1;
    if (entry.wo_id) {
      const row = summary.byWorkOrder.get(entry.wo_id) || { pending: 0, problems: 0 };
      if (problem) row.problems += 1;
      else row.pending += 1;
      summary.byWorkOrder.set(entry.wo_id, row);
    }
    if (entry.kind === "finding" && entry.item_id) summary.findings.set(entry.item_id, entry);
    if (entry.item_id) {
      const row = summary.byItem.get(entry.item_id) || { pending: 0, problems: 0 };
      if (problem) row.problems += 1;
      else row.pending += 1;
      summary.byItem.set(entry.item_id, row);
    }
  }
  return summary;
}
