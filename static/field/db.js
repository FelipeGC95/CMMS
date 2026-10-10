// IndexedDB de la vista de terreno (ADR-011): `galbust-field` v1.
//   meta         "session": user_id, organization_id, synced_at, client_version y bootstrap;
//                "timer:<OT>": tramos de trabajo locales (iniciar/pausar) para precargar los minutos;
//                "recent": últimas solicitudes enviadas (con su número)
//   work_orders  una OT por registro, con su checklist incrustado (contrato `snapshot`)
//   reference    catálogos (códigos de falla, activos, medidores) en el registro "catalog"
//   queue        cambios pendientes de enviar (clave client_uuid; índices wo_id y status)
//   blobs        fotos tomadas sin conexión, hasta que se envían
// Cada registro guarda user_id y organization_id: los datos de otra persona u organización
// nunca se muestran ni se envían con la sesión actual.

export const DB_NAME = "galbust-field";
export const DB_VERSION = 1;
export const STORES = ["meta", "work_orders", "reference", "queue", "blobs"];
// Datos más viejos que esto no se muestran: se borran y hay que descargar de nuevo.
export const MAX_SNAPSHOT_AGE_MS = 14 * 24 * 60 * 60 * 1000;
const SESSION_KEY = "session";
const RECENT_KEY = "recent";
const CATALOG_KEY = "catalog";
const RECENT_MAX = 5;

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
      request.onsuccess = () => {
        const db = request.result;
        // "Salir" o un cambio de organización (static/js/app.js, en esta u otra pestaña)
        // borra la base: se cierra la conexión para no bloquearlo y la vista se recarga vacía.
        db.onversionchange = (event) => {
          db.close();
          dbPromise = null;
          if (event.newVersion === null && !document.documentElement.dataset.fieldExiting) location.reload();
        };
        resolve(db);
      };
      request.onerror = () => reject(request.error);
    });
  }
  return dbPromise;
}

async function get(store, key) {
  const db = await openDb();
  return promisify(db.transaction(store).objectStore(store).get(key));
}

async function getAll(store) {
  const db = await openDb();
  return promisify(db.transaction(store).objectStore(store).getAll());
}

async function put(store, value) {
  const db = await openDb();
  const tx = db.transaction(store, "readwrite");
  tx.objectStore(store).put(value);
  return done(tx);
}

async function remove(store, key) {
  const db = await openDb();
  const tx = db.transaction(store, "readwrite");
  tx.objectStore(store).delete(key);
  return done(tx);
}

const owned = (record, identity) =>
  Boolean(record && identity) &&
  record.user_id === identity.user_id &&
  record.organization_id === identity.organization_id;

// ---------- Sesión y datos descargados ----------

export async function getSession() {
  return get("meta", SESSION_KEY);
}

export function identityOf(bootstrap) {
  return {
    user_id: bootstrap.user.id,
    organization_id: bootstrap.organization.id,
    organization_name: bootstrap.organization.name,
  };
}

export async function listWorkOrders(identity) {
  const rows = await getAll("work_orders");
  return rows.filter((row) => owned(row, identity)).sort((a, b) => a._position - b._position);
}

export async function getWorkOrder(id, identity) {
  const row = await get("work_orders", id);
  return owned(row, identity) ? row : undefined;
}

export async function putWorkOrder(workOrder) {
  return put("work_orders", workOrder);
}

// Guarda quién es la persona y qué puede hacer (respuesta de `bootstrap`).
export async function saveBootstrap(bootstrap, clientVersion) {
  const previous = await getSession();
  const identity = identityOf(bootstrap);
  await put("meta", {
    ...(previous || {}),
    key: SESSION_KEY,
    user_id: identity.user_id,
    organization_id: identity.organization_id,
    client_version: clientVersion,
    bootstrap,
    synced_at: (previous && owned(previous, identity) && previous.synced_at) || null,
  });
}

// Reemplaza las OT descargadas (no toca la cola ni las fotos pendientes).
export async function saveSnapshot(snapshot, identity) {
  const db = await openDb();
  const tx = db.transaction(["meta", "work_orders"], "readwrite");
  const workOrders = tx.objectStore("work_orders");
  workOrders.clear();
  snapshot.work_orders.forEach((wo, position) =>
    workOrders.put({
      ...wo,
      _position: position,
      user_id: identity.user_id,
      organization_id: identity.organization_id,
    }),
  );
  const meta = tx.objectStore("meta");
  const request = meta.get(SESSION_KEY);
  request.onsuccess = () => {
    const session = request.result;
    if (session) meta.put({ ...session, synced_at: new Date().toISOString(), truncated: Boolean(snapshot.truncated) });
  };
  return done(tx);
}

export async function saveReference(reference, identity) {
  return put("reference", { key: CATALOG_KEY, ...identity, ...reference });
}

export async function getReference(identity) {
  const row = await get("reference", CATALOG_KEY);
  return owned(row, identity) ? row : null;
}

// Borra los datos de la organización (OT, catálogos, sesión y tramos de trabajo).
// La cola y sus fotos se conservan: pueden ser de otra persona y se descartan solo a mano (ADR-011).
export async function clearData() {
  const db = await openDb();
  const tx = db.transaction(["meta", "work_orders", "reference"], "readwrite");
  ["meta", "work_orders", "reference"].forEach((name) => tx.objectStore(name).clear());
  return done(tx);
}

export function isStale(session, now = Date.now()) {
  return Boolean(session && session.synced_at) && now - Date.parse(session.synced_at) > MAX_SNAPSHOT_AGE_MS;
}

// ---------- Tramos de trabajo locales (minutos al completar) ----------

export async function getTimer(woId, identity) {
  const row = await get("meta", `timer:${woId}`);
  return owned(row, identity) ? row : { key: `timer:${woId}`, ...identity, spans: [] };
}

export async function saveTimer(timer) {
  return put("meta", timer);
}

export async function deleteTimer(woId) {
  return remove("meta", `timer:${woId}`);
}

// ---------- Solicitudes enviadas (número que devuelve el servidor) ----------

export async function listRecent(identity) {
  const row = await get("meta", RECENT_KEY);
  return owned(row, identity) ? row.items : [];
}

export async function addRecent(item, identity) {
  const items = [item, ...(await listRecent(identity))].slice(0, RECENT_MAX);
  return put("meta", { key: RECENT_KEY, ...identity, items });
}

// ---------- Cola de cambios ----------

let sequence = 0;
export function nextSeq() {
  sequence = (sequence + 1) % 1000;
  return Date.now() * 1000 + sequence;
}

export async function listQueue() {
  const rows = await getAll("queue");
  return rows.sort((a, b) => a.seq - b.seq);
}

export async function getEntry(clientUuid) {
  return get("queue", clientUuid);
}

export async function putEntry(entry) {
  return put("queue", entry);
}

// Quita el cambio y sus fotos (enviado o descartado).
export async function deleteEntry(entry) {
  const db = await openDb();
  const tx = db.transaction(["queue", "blobs"], "readwrite");
  tx.objectStore("queue").delete(entry.client_uuid);
  (entry.blob_ids || []).forEach((id) => tx.objectStore("blobs").delete(id));
  return done(tx);
}

// ---------- Fotos ----------

export async function putBlob(record) {
  return put("blobs", record);
}

export async function getBlob(id) {
  return get("blobs", id);
}
