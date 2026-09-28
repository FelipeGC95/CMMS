// Entrada de la vista de terreno (ADR-011): módulo ES sin build. Enrutamiento por hash:
//   #/            OT agrupadas por día (asignadas a la persona primero)
//   #/wo/<id>     detalle de una OT (solo lectura en esta etapa)
// Los datos salen de IndexedDB. La descarga desde /field/api/ y la cola de cambios llegan después (P2/P4).
import * as db from "./db.js";
import * as ui from "./ui.js";

export const CLIENT_VERSION = 1;

const body = document.body;
const view = document.getElementById("field-view");
const status = document.getElementById("field-status");
const updateBanner = document.getElementById("field-update");

async function loadSession() {
  const session = await db.getSession();
  if (session && db.isStale(session)) {
    await db.clearData();
    return null;
  }
  return session;
}

async function render() {
  const session = await loadSession();
  const route = location.hash.replace(/^#/, "") || "/";
  if (!session) {
    ui.renderTemplate(view, "tpl-empty");
  } else {
    const match = route.match(/^\/wo\/([^/]+)$/);
    if (match) {
      const workOrder = await db.getWorkOrder(decodeURIComponent(match[1]));
      if (workOrder) ui.renderDetail(view, { session, workOrder });
      else ui.renderTemplate(view, "tpl-not-found");
    } else {
      ui.renderList(view, { session, workOrders: await db.listWorkOrders() });
    }
  }
  ui.focusHeading(view);
}

// Datos de demostración (solo con DEBUG): el mismo contrato que /field/api/bootstrap y snapshot.
async function loadDemo() {
  const response = await fetch(body.dataset.demoUrl, { credentials: "same-origin" });
  if (!response.ok) return;
  await db.saveSnapshot(await response.json(), CLIENT_VERSION);
  location.hash = "#/";
  await render();
}

function showConnection() {
  ui.renderConnection(status, navigator.onLine);
}

// Versión nueva del Service Worker en espera: se ofrece "Actualizar".
function watchUpdates(registration) {
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
  updateBanner.querySelector("[data-action='update']").addEventListener("click", () => {
    if (registration.waiting) registration.waiting.postMessage({ type: "SKIP_WAITING" });
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

document.addEventListener("click", (event) => {
  if (event.target.closest("[data-action='load-demo']")) loadDemo();
});
window.addEventListener("hashchange", render);
window.addEventListener("online", showConnection);
window.addEventListener("offline", showConnection);

showConnection();
render();
registerServiceWorker();
