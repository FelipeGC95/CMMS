// Comportamientos mínimos sin JS en línea (CSP estricta).

// Con JS el selector de organización cambia solo al elegir: el botón "Cambiar" queda para sin JS.
document.documentElement.classList.add("js");
document.addEventListener("change", (event) => {
  // Cambiar de organización al elegirla (en móvil no se muestra el botón "Cambiar").
  if (event.target.matches(".org-switch select")) {
    event.target.form.requestSubmit();
  }
});

// Selector de tema: Sistema → Claro → Oscuro.
const THEME_ORDER = ["system", "light", "dark"];

function renderThemeButtons() {
  const pref = window.cmmsTheme.get();
  document.querySelectorAll("[data-theme-cycle]").forEach((button) => {
    const label = button.dataset[`label${pref.charAt(0).toUpperCase()}${pref.slice(1)}`];
    button.querySelector("[data-theme-label]").textContent = label;
    button.setAttribute("aria-label", `${button.dataset.labelPrefix}: ${label}`);
  });
}

document.addEventListener("click", (event) => {
  const button = event.target.closest("[data-theme-cycle]");
  if (!button) return;
  const next = THEME_ORDER[(THEME_ORDER.indexOf(window.cmmsTheme.get()) + 1) % THEME_ORDER.length];
  window.cmmsTheme.set(next);
  renderThemeButtons();
});

document.addEventListener("DOMContentLoaded", renderThemeButtons);

// ---------- Vista de terreno sin conexión (ADR-011 §1e) ----------
// La PWA guarda OT y cambios pendientes en IndexedDB (`galbust-field`).
//   Salir: se borra la base completa; si quedan cambios sin enviar, antes se pide confirmar.
//   Cambiar de organización: sin cambios pendientes se borra la base; con pendientes se
//   confirma y se borran solo los datos descargados (la cola espera a que se vuelva a esa
//   organización, ADR-011 §1a).
//   Cualquier página de la web: si los datos de /field/ son de otra persona u organización,
//   se borran los datos descargados.
// Sin Clear-Site-Data: el tema y la estructura de la aplicación (Service Worker) se conservan.
const FIELD_DB = "galbust-field";
const FIELD_DATA_STORES = ["meta", "work_orders", "reference"];

async function fieldDbExists() {
  if (!window.indexedDB) return false;
  if (!indexedDB.databases) return true; // sin la lista: se intenta abrir sin crearla
  try {
    return (await indexedDB.databases()).some((info) => info.name === FIELD_DB);
  } catch (error) {
    return true;
  }
}

// Abre la base solo si ya existe: si no existía, se aborta su creación.
function openFieldDb() {
  return new Promise((resolve) => {
    let request;
    try {
      request = indexedDB.open(FIELD_DB);
    } catch (error) {
      resolve(null);
      return;
    }
    request.onupgradeneeded = () => request.transaction.abort();
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => resolve(null);
    request.onblocked = () => resolve(null);
  });
}

function idbRequest(request) {
  return new Promise((resolve, reject) => {
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
}

// Cuántos cambios esperan en la cola y de quién son los datos descargados.
async function fieldState() {
  if (!(await fieldDbExists())) return null;
  const db = await openFieldDb();
  if (!db) return null;
  try {
    const has = (name) => db.objectStoreNames.contains(name);
    const pending = has("queue") ? await idbRequest(db.transaction("queue").objectStore("queue").count()) : 0;
    const session = has("meta") ? await idbRequest(db.transaction("meta").objectStore("meta").get("session")) : null;
    return { pending, session: session || null };
  } finally {
    db.close();
  }
}

// Borra la base completa (OT, catálogos, cola y fotos). Las pestañas de /field/ abiertas
// cierran su conexión al recibir `versionchange` (static/field/db.js).
function deleteFieldDb() {
  return new Promise((resolve) => {
    const timer = setTimeout(resolve, 3000);
    const finish = () => {
      clearTimeout(timer);
      resolve();
    };
    const request = indexedDB.deleteDatabase(FIELD_DB);
    request.onsuccess = finish;
    request.onerror = finish;
  });
}

// Borra lo descargado (OT, catálogos, sesión); conserva la cola y sus fotos.
async function clearFieldData() {
  const db = await openFieldDb();
  if (!db) return;
  try {
    const names = FIELD_DATA_STORES.filter((name) => db.objectStoreNames.contains(name));
    if (!names.length) return;
    const tx = db.transaction(names, "readwrite");
    names.forEach((name) => tx.objectStore(name).clear());
    await new Promise((resolve) => {
      tx.oncomplete = resolve;
      tx.onerror = resolve;
      tx.onabort = resolve;
    });
  } finally {
    db.close();
  }
}

function fieldPendingText(dialog, mode, count) {
  const key = `msg${mode.charAt(0).toUpperCase()}${mode.slice(1)}${count === 1 ? "One" : "Other"}`;
  return (dialog.dataset[key] || "").replace("{n}", String(count));
}

// Confirmación accesible (<dialog> modal con foco atrapado y Esc para cerrar):
// resuelve "confirm", o "" si se cancela.
function confirmFieldPending(mode, count) {
  const dialog = document.getElementById("field-exit-dialog");
  if (!dialog || typeof dialog.showModal !== "function") {
    return Promise.resolve(dialog && window.confirm(fieldPendingText(dialog, mode, count)) ? "confirm" : "");
  }
  dialog.querySelector("[data-slot='text']").textContent = fieldPendingText(dialog, mode, count);
  dialog.querySelectorAll("[data-mode]").forEach((node) => {
    node.hidden = node.dataset.mode !== mode;
  });
  dialog.returnValue = "";
  dialog.showModal();
  return new Promise((resolve) => {
    dialog.addEventListener("close", () => resolve(dialog.returnValue), { once: true });
  });
}

function csrfFromCookie() {
  const match = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/);
  return match ? decodeURIComponent(match[1]) : "";
}

// Si se cancela el cambio de organización, el selector vuelve a la organización activa.
function resetSelect(form) {
  const select = form.querySelector("select");
  if (!select) return;
  const original = [...select.options].find((option) => option.defaultSelected);
  if (original) select.value = original.value;
}

async function beforeFieldExit(form) {
  const state = await fieldState().catch(() => null);
  if (!state) return true;
  if (form.hasAttribute("data-field-exit")) {
    if (state.pending && (await confirmFieldPending("exit", state.pending)) !== "confirm") return false;
    document.documentElement.dataset.fieldExiting = "1";
    await deleteFieldDb();
    return true;
  }
  const target = form.elements.organization ? form.elements.organization.value : "";
  if (state.session && String(state.session.organization_id) === target) return true;
  if (state.pending) {
    if ((await confirmFieldPending("switch", state.pending)) !== "confirm") return false;
    await clearFieldData();
  } else {
    document.documentElement.dataset.fieldExiting = "1";
    await deleteFieldDb();
  }
  return true;
}

document.addEventListener("submit", (event) => {
  const form = event.target.closest("form[data-field-exit], form[data-field-switch]");
  if (!form || form.dataset.fieldReady) return;
  event.preventDefault();
  if (form.dataset.fieldBusy) return;
  form.dataset.fieldBusy = "1";
  beforeFieldExit(form)
    .catch(() => true)
    .then((proceed) => {
      delete form.dataset.fieldBusy;
      if (!proceed) {
        resetSelect(form);
        return;
      }
      // La página de terreno se guarda sin token CSRF: se toma de la cookie al enviar.
      const csrf = form.querySelector("[data-csrf-cookie]");
      if (csrf) csrf.value = csrfFromCookie();
      form.dataset.fieldReady = "1";
      form.submit();
    });
});

async function forgetOtherFieldData() {
  const { userId, organizationId } = document.body.dataset;
  if (!userId || !organizationId) return;
  const state = await fieldState();
  const session = state && state.session;
  if (session && (String(session.user_id) !== userId || String(session.organization_id) !== organizationId)) {
    await clearFieldData();
  }
}

document.addEventListener("DOMContentLoaded", () => {
  forgetOtherFieldData().catch(() => {});
});
