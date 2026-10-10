// Cliente de /field/api/ (ADR-011): sesión de la web + CSRF del mismo origen.
// El token CSRF se lee de la cookie en cada envío (nunca se guarda en la cola) y toda
// petición declara la versión del cliente: una versión vieja recibe 409 `upgrade_required`.

export const CLIENT_VERSION = 1;

let base = "/field/api/";

export function configure(apiUrl) {
  base = apiUrl;
}

function csrfToken() {
  const match = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/);
  return match ? decodeURIComponent(match[1]) : "";
}

// Error de la API con la forma {"error": {"code", "message", "details"}}.
export class ApiError extends Error {
  constructor(status, body) {
    const error = (body && body.error) || {};
    super(error.message || `HTTP ${status}`);
    this.status = status;
    this.code = error.code || (status === 0 ? "network" : "error");
    this.details = error.details || {};
  }

  // Qué hace la cola con este error (ADR-011 §1e).
  get kind() {
    if (this.status === 0 || this.status >= 500 || this.status === 429) return "retry";
    if (this.status === 401) return "auth";
    if (this.status === 403 && this.code === "mfa_required") return "mfa";
    // Un CSRF rechazado es una sesión cambiada: se pide entrar de nuevo, no es un conflicto.
    if (this.status === 403 && /CSRF/i.test(this.message)) return "auth";
    if (this.status === 409 && this.code === "upgrade_required") return "upgrade";
    if (this.status === 400 || this.status === 413) return "rejected";
    return "conflict"; // 404, 409 y 403 de permisos: no se reintenta
  }
}

async function request(method, path, { json, form, signal } = {}) {
  const headers = { "X-Client-Version": String(CLIENT_VERSION), Accept: "application/json" };
  let body;
  if (json !== undefined) {
    headers["Content-Type"] = "application/json";
    body = JSON.stringify(json);
  } else if (form !== undefined) {
    body = form; // el navegador fija el boundary de multipart/form-data
  }
  if (method !== "GET") headers["X-CSRFToken"] = csrfToken();
  let response;
  try {
    response = await fetch(`${base}${path}`, {
      method,
      headers,
      body,
      credentials: "same-origin",
      cache: "no-store",
      redirect: "error",
      signal,
    });
  } catch (error) {
    throw new ApiError(0, null);
  }
  let data = null;
  const type = response.headers.get("Content-Type") || "";
  if (type.includes("json")) {
    try {
      data = await response.json();
    } catch (error) {
      data = null;
    }
  }
  if (!response.ok) throw new ApiError(response.status, data);
  return { status: response.status, data };
}

export const get = (path) => request("GET", path).then((result) => result.data);
export const postJson = (path, json) => request("POST", path, { json });
export const postForm = (path, form) => request("POST", path, { form });
