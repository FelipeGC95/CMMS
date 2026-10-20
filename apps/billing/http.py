"""HTTP saliente del cobro (Mercado Pago y la fuente de la UF): JSON, timeout corto y defensa SSRF.

Reutiliza la conexión fijada a una IP pública ya validada de los webhooks (ADR-030): sin
redirecciones, solo https y respuesta acotada. Nunca registra cabeceras (llevan el token).
"""

import http.client
import json
import ssl
from urllib.parse import urlsplit

from apps.webhooks import services as outbound

TIMEOUT_SECONDS = 10
MAX_RESPONSE_BYTES = 1024 * 1024


class HttpError(Exception):
    """Falla de red, de TLS, respuesta no JSON o demasiado grande. `status` = 0 sin respuesta."""

    def __init__(self, message: str, *, status: int = 0, data=None):
        self.status = status
        self.data = data
        super().__init__(message)


def request_json(method: str, url: str, *, headers=None, payload=None, timeout=TIMEOUT_SECONDS):
    """Pide `url` y devuelve (código, JSON). Lanza `HttpError` si no hay respuesta JSON válida."""
    try:
        address = outbound.public_address(url)
    except outbound.UnsafeURLError as error:
        raise HttpError(error.message) from error
    parts = urlsplit(url)
    path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    body = None if payload is None else json.dumps(payload).encode()
    all_headers = {"Accept": "application/json", "User-Agent": "GALBUST-CMMS-Billing", **(headers or {})}
    if body is not None:
        all_headers["Content-Type"] = "application/json"
    connection = outbound.PinnedHTTPSConnection(
        parts.hostname,
        address=address,
        port=parts.port or 443,
        timeout=timeout,
        context=ssl.create_default_context(),
    )
    try:
        connection.request(method, path, body=body, headers=all_headers)
        response = connection.getresponse()
        raw = response.read(MAX_RESPONSE_BYTES + 1)
    except (OSError, http.client.HTTPException) as error:
        raise HttpError(f"{type(error).__name__} al conectar con {parts.hostname}") from error
    finally:
        connection.close()
    if len(raw) > MAX_RESPONSE_BYTES:
        raise HttpError("Respuesta demasiado grande", status=response.status)
    try:
        data = json.loads(raw) if raw else {}
    except ValueError as error:
        raise HttpError("Respuesta no JSON", status=response.status) from error
    return response.status, data
