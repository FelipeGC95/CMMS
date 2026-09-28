// IndexedDB de la vista de terreno (ADR-011): `galbust-field` v1.
//   meta         clave "session": user_id, organization_id, synced_at, client_version y bootstrap
//   work_orders  una OT por registro, con su checklist incrustado (contrato `snapshot`)
//   reference    catálogos (códigos de falla, activos, medidores) por clave
//   queue        cambios pendientes de enviar; cada uno con user_id y organization_id (P4)
//   blobs        fotos tomadas sin conexión (P4)

export const DB_NAME = "galbust-field";
export const DB_VERSION = 1;
export const STORES = ["meta", "work_orders", "reference", "queue", "blobs"];
// Datos más viejos que esto no se muestran: se borran y hay que descargar de nuevo.
export const MAX_SNAPSHOT_AGE_MS = 14 * 24 * 60 * 60 * 1000;
const SESSION_KEY = "session";

let dbPromise = null;

function upgrade(db) {
  if (!db.objectStoreNames.contains("meta")) db.createObjectStore("meta", { keyPath: "key" });
  if (!db.objectStoreNames.contains("work_orders")) db.createObjectStore("work_orders", { keyPath: "id" });
  if (!db.objectStoreNames.contains("reference")) db.createObjectStore("reference", { keyPath: "key" });
  if (!db.objectStoreNames.contains("queue")) {
    const queue = db.createObjectStore("queue", { keyPath: "client_uuid" });
    queue.createIndex("wo_id", "wo_id");
    queue.createIndex("status", "status");
  }
  if (!db.objectStoreNames.contains("blobs")) db.createObjectStore("blobs", { keyPath: "id" });
}

function promisify(request) {
  return new Promise((resolve, reject) => {
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
}

function done(tx) {
  return new Promise((resolve, reject) => {
    tx.oncomplete = () => resolve();
    tx.onerror = () => reject(tx.error);
    tx.onabort = () => reject(tx.error);
  });
}

export function openDb() {
  if (!dbPromise) {
    dbPromise = new Promise((resolve, reject) => {
      const request = indexedDB.open(DB_NAME, DB_VERSION);
      request.onupgradeneeded = () => upgrade(request.result);
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(request.error);
    });
  }
  return dbPromise;
}

export async function getSession() {
  const db = await openDb();
  return promisify(db.transaction("meta").objectStore("meta").get(SESSION_KEY));
}

export async function listWorkOrders() {
  const db = await openDb();
  const rows = await promisify(db.transaction("work_orders").objectStore("work_orders").getAll());
  return rows.sort((a, b) => a._position - b._position);
}

export async function getWorkOrder(id) {
  const db = await openDb();
  return promisify(db.transaction("work_orders").objectStore("work_orders").get(id));
}

// Reemplaza los datos descargados (no toca la cola ni las fotos pendientes).
export async function saveSnapshot({ bootstrap, snapshot }, clientVersion) {
  const db = await openDb();
  const tx = db.transaction(["meta", "work_orders"], "readwrite");
  const workOrders = tx.objectStore("work_orders");
  workOrders.clear();
  snapshot.work_orders.forEach((wo, position) => workOrders.put({ ...wo, _position: position }));
  tx.objectStore("meta").put({
    key: SESSION_KEY,
    user_id: bootstrap.user.id,
    organization_id: bootstrap.organization.id,
    synced_at: new Date().toISOString(),
    client_version: clientVersion,
    truncated: Boolean(snapshot.truncated),
    bootstrap,
  });
  return done(tx);
}

// Borra los datos de la organización (OT, catálogos y sesión). La cola se conserva (ADR-011).
export async function clearData() {
  const db = await openDb();
  const tx = db.transaction(["meta", "work_orders", "reference"], "readwrite");
  ["meta", "work_orders", "reference"].forEach((name) => tx.objectStore(name).clear());
  return done(tx);
}

export function isStale(session, now = Date.now()) {
  return !session || now - Date.parse(session.synced_at) > MAX_SNAPSHOT_AGE_MS;
}
