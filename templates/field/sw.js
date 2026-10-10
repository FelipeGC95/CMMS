{% autoescape off %}// Service Worker de la vista de terreno (ADR-011). Alcance: /field/.
// Solo guarda la estructura de la aplicación; los datos viven en IndexedDB.
// Nunca pasa por la caché: /field/api/, /files/, /accounts/, /org-theme.css, POST ni nada fuera de la precarga.
const CACHE_VERSION = "{{ cache_version }}";
const CACHE = `field-${CACHE_VERSION}`;
const SHELL_URL = {{ shell_url }};
const PRECACHE = {{ precache }};
const PRECACHED = new Set(PRECACHE.map((url) => new URL(url, self.location.origin).href));
const NETWORK_TIMEOUT_MS = 3000;

self.addEventListener("install", (event) => {
  // La versión nueva queda en espera: la página ofrece "Actualizar" (sin cortar un envío en curso).
  event.waitUntil(
    caches.open(CACHE).then((cache) =>
      cache.addAll(PRECACHE.map((url) => new Request(url, { cache: "reload", credentials: "same-origin" }))),
    ),
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) => Promise.all(keys.filter((key) => key.startsWith("field-") && key !== CACHE).map((key) => caches.delete(key))))
      .then(() => self.clients.claim()),
  );
});

self.addEventListener("message", (event) => {
  if (event.data && event.data.type === "SKIP_WAITING") self.skipWaiting();
});

function isShellNavigation(request, url) {
  return request.mode === "navigate" && url.pathname === SHELL_URL;
}

async function networkFirstShell(request) {
  const cache = await caches.open(CACHE);
  const network = fetch(request);
  const timeout = new Promise((resolve) => setTimeout(resolve, NETWORK_TIMEOUT_MS, null));
  try {
    const response = await Promise.race([network, timeout]);
    if (response) {
      // Solo la página base real (200, sin redirigir al login) reemplaza la guardada.
      if (response.ok && response.type === "basic" && !response.redirected) {
        await cache.put(SHELL_URL, response.clone());
      }
      return response;
    }
  } catch (error) {
    // Sin red: se usa la copia guardada.
  }
  const cached = await cache.match(SHELL_URL);
  return cached || network;
}

async function cacheFirst(request) {
  const cached = await caches.match(request, { cacheName: CACHE });
  return cached || fetch(request);
}

self.addEventListener("fetch", (event) => {
  const { request } = event;
  if (request.method !== "GET") return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;
  if (isShellNavigation(request, url)) {
    event.respondWith(networkFirstShell(request));
    return;
  }
  if (request.mode !== "navigate" && PRECACHED.has(url.href)) {
    event.respondWith(cacheFirst(request));
  }
});
{% endautoescape %}