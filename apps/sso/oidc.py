"""Cliente OpenID Connect mínimo (ORG-11, ADR-032): authorization code + PKCE, state y nonce.

Solo protocolo, sin base de datos. Todo pedido HTTP pasa por `http_request`, que reutiliza la
defensa SSRF de los webhooks (ADR-030): https, dominio con direcciones públicas, conexión a la
IP ya validada y sin seguir redirecciones. Los tests reemplazan `http_request` por un IdP falso.

El id_token se valida siempre con la firma (JWKS del proveedor, solo algoritmos asimétricos),
el emisor, la audiencia, la vigencia y el nonce, aunque llegue por el canal directo del token.
"""

import base64
import hashlib
import http.client
import json
import secrets
import ssl
from urllib.parse import urlencode, urlsplit

import jwt
from django.utils.translation import gettext as _

from apps.webhooks import services as outbound

DISCOVERY_SUFFIX = "/.well-known/openid-configuration"
SCOPES = "openid email profile"
TIMEOUT_SECONDS = 5
MAX_RESPONSE_BYTES = 512 * 1024
LEEWAY_SECONDS = 60
# Sin HS256 (usaría el secreto del cliente como clave) ni `none`.
ALLOWED_ALGORITHMS = frozenset(
    {"RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "ES512"}
)
# Lo que se guarda del documento de descubrimiento.
METADATA_KEYS = (
    "issuer",
    "authorization_endpoint",
    "token_endpoint",
    "jwks_uri",
    "userinfo_endpoint",
    "token_endpoint_auth_methods_supported",
)


class SsoError(Exception):
    """Falla del SSO; `message` se muestra a la persona."""

    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


# --- HTTP ---------------------------------------------------------------------------------


def http_request(method: str, url: str, *, body: bytes | None = None, headers=None) -> tuple[int, bytes]:
    """Pedido HTTPS con la defensa SSRF de los webhooks; devuelve (código, cuerpo)."""
    try:
        address = outbound.public_address(url)
    except outbound.UnsafeURLError as error:
        raise SsoError(error.message) from error
    parts = urlsplit(url)
    path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    connection = outbound.PinnedHTTPSConnection(
        parts.hostname,
        address=address,
        port=parts.port or 443,
        timeout=TIMEOUT_SECONDS,
        context=ssl.create_default_context(),
    )
    try:
        connection.request(
            method,
            path,
            body=body,
            headers={"Accept": "application/json", "User-Agent": "GALBUST-CMMS-SSO", **(headers or {})},
        )
        response = connection.getresponse()
        data = response.read(MAX_RESPONSE_BYTES + 1)
    except (OSError, http.client.HTTPException) as error:
        raise SsoError(_("No se pudo conectar con el proveedor de identidad.")) from error
    finally:
        connection.close()
    if len(data) > MAX_RESPONSE_BYTES:
        raise SsoError(_("El proveedor de identidad respondió demasiados datos."))
    return response.status, data


def _json(status: int, data: bytes, what: str) -> dict:
    if status != 200:
        raise SsoError(
            _("El proveedor de identidad respondió %(status)s al pedir %(what)s.")
            % {"status": status, "what": what}
        )
    try:
        value = json.loads(data)
    except ValueError as error:
        raise SsoError(
            _("El proveedor de identidad no respondió JSON al pedir %(what)s.") % {"what": what}
        ) from error
    if not isinstance(value, dict):
        raise SsoError(_("El proveedor de identidad no respondió JSON al pedir %(what)s.") % {"what": what})
    return value


def get_json(url: str, *, what: str, headers=None) -> dict:
    return _json(*http_request("GET", url, headers=headers), what)


# --- Descubrimiento -----------------------------------------------------------------------


def normalize_discovery_url(url: str) -> str:
    """Acepta el emisor o la URL completa de descubrimiento; devuelve la de descubrimiento."""
    url = url.strip()
    if url.endswith(DISCOVERY_SUFFIX):
        return url
    return url.rstrip("/") + DISCOVERY_SUFFIX


def _is_https(value) -> bool:
    return isinstance(value, str) and urlsplit(value).scheme == "https" and bool(urlsplit(value).hostname)


def fetch_metadata(discovery_url: str) -> dict:
    """Lee y valida el documento de descubrimiento (OpenID Connect Discovery 1.0 §4)."""
    if urlsplit(discovery_url).scheme != "https":
        raise SsoError(_("La URL del proveedor debe empezar con https://."))
    document = get_json(discovery_url, what=_("el documento de descubrimiento"))
    for key in ("issuer", "authorization_endpoint", "token_endpoint", "jwks_uri"):
        if not _is_https(document.get(key)):
            raise SsoError(_("El documento de descubrimiento no trae %(key)s con https.") % {"key": key})
    issuer = document["issuer"]
    # El emisor debe ser exactamente el de la URL (§4.3). Así también se rechazan los
    # endpoints multiempresa como `common` de Entra ID, cuyo emisor es una plantilla.
    if "{" in issuer or issuer.rstrip("/") + DISCOVERY_SUFFIX != discovery_url:
        raise SsoError(
            _("El emisor (%(issuer)s) no corresponde a la URL. Usa la URL de tu inquilino, no una genérica.")
            % {"issuer": issuer}
        )
    if "code" not in document.get("response_types_supported", ["code"]):
        raise SsoError(_("El proveedor no admite el flujo authorization code."))
    if document.get("userinfo_endpoint") and not _is_https(document["userinfo_endpoint"]):
        raise SsoError(
            _("El documento de descubrimiento no trae %(key)s con https.") % {"key": "userinfo_endpoint"}
        )
    return {key: document[key] for key in METADATA_KEYS if key in document}


def fetch_jwks(metadata: dict) -> dict:
    jwks = get_json(metadata["jwks_uri"], what=_("las claves públicas (JWKS)"))
    if not isinstance(jwks.get("keys"), list) or not jwks["keys"]:
        raise SsoError(_("El proveedor no publicó claves para firmar."))
    return jwks


# --- Autorización -------------------------------------------------------------------------


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def new_flow() -> dict:
    """state, nonce y verificador PKCE de un intento (se guardan en la sesión)."""
    return {
        "state": secrets.token_urlsafe(32),
        "nonce": secrets.token_urlsafe(32),
        "code_verifier": secrets.token_urlsafe(64),  # 86 caracteres (RFC 7636: 43 a 128)
    }


def code_challenge(verifier: str) -> str:
    return _b64url(hashlib.sha256(verifier.encode()).digest())


def authorization_url(*, metadata: dict, client_id: str, redirect_uri: str, flow: dict, login_hint="") -> str:
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": SCOPES,
        "state": flow["state"],
        "nonce": flow["nonce"],
        "code_challenge": code_challenge(flow["code_verifier"]),
        "code_challenge_method": "S256",
    }
    if login_hint:
        params["login_hint"] = login_hint
    endpoint = metadata["authorization_endpoint"]
    separator = "&" if urlsplit(endpoint).query else "?"
    return f"{endpoint}{separator}{urlencode(params)}"


# --- Token e id_token ---------------------------------------------------------------------


def exchange_code(
    *, metadata: dict, client_id: str, client_secret: str, code: str, redirect_uri: str, code_verifier: str
) -> dict:
    """Canjea el código por tokens (RFC 6749 §4.1.3 + PKCE). Debe venir un id_token."""
    form = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": client_id,
        "code_verifier": code_verifier,
    }
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    methods = metadata.get("token_endpoint_auth_methods_supported") or ["client_secret_basic"]
    if "client_secret_post" in methods:
        form["client_secret"] = client_secret
    else:
        # RFC 6749 §2.3.1: id y secreto codificados como formulario antes de Basic.
        pair = f"{_form_quote(client_id)}:{_form_quote(client_secret)}"
        headers["Authorization"] = "Basic " + base64.b64encode(pair.encode()).decode()
    status, data = http_request(
        "POST", metadata["token_endpoint"], body=urlencode(form).encode(), headers=headers
    )
    tokens = _json(status, data, _("los tokens"))
    if not isinstance(tokens.get("id_token"), str):
        raise SsoError(_("El proveedor no entregó un id_token."))
    return tokens


def _form_quote(value: str) -> str:
    return urlencode({"v": value})[2:]


def _candidate_keys(jwks: dict, header: dict) -> list:
    algorithm = header.get("alg")
    if algorithm not in ALLOWED_ALGORITHMS:
        raise SsoError(_("El id_token usa un algoritmo de firma no permitido."))
    kid = header.get("kid")
    keys = []
    for data in jwks.get("keys", []):
        if not isinstance(data, dict) or data.get("use", "sig") != "sig":
            continue
        if kid is not None and data.get("kid") != kid:
            continue
        if data.get("alg") not in (None, algorithm):
            continue
        try:
            keys.append(jwt.PyJWK(data, algorithm=algorithm))
        except (jwt.exceptions.PyJWKError, jwt.exceptions.InvalidKeyError):
            continue
    if not keys:
        raise SsoError(_("No se encontró la clave pública que firmó el id_token."))
    return keys


def validate_id_token(*, id_token: str, jwks: dict, issuer: str, client_id: str, nonce: str) -> dict:
    """Firma, emisor, audiencia, vigencia y nonce (OIDC Core §3.1.3.7)."""
    try:
        header = jwt.get_unverified_header(id_token)
    except jwt.exceptions.DecodeError as error:
        raise SsoError(_("El id_token no es válido.")) from error
    claims = None
    last_error = None
    for key in _candidate_keys(jwks, header):
        try:
            claims = jwt.decode(
                id_token,
                key=key.key,
                algorithms=[header["alg"]],
                audience=client_id,
                issuer=issuer,
                leeway=LEEWAY_SECONDS,
                options={"require": ["exp", "iat", "iss", "aud", "sub"]},
            )
            break
        except jwt.exceptions.InvalidSignatureError as error:
            last_error = error
        except jwt.exceptions.PyJWTError as error:
            raise SsoError(_("El id_token no es válido: %(error)s.") % {"error": error}) from error
    if claims is None:
        raise SsoError(_("La firma del id_token no es válida.")) from last_error
    audiences = claims["aud"] if isinstance(claims["aud"], list) else [claims["aud"]]
    if len(audiences) > 1 and claims.get("azp") != client_id:
        raise SsoError(_("El id_token no fue emitido para esta aplicación."))
    if not isinstance(claims.get("nonce"), str) or not secrets.compare_digest(claims["nonce"], nonce):
        raise SsoError(_("El id_token no corresponde a este intento de inicio de sesión (nonce)."))
    return claims


def fetch_userinfo(*, metadata: dict, access_token: str, subject: str) -> dict:
    """Datos del usuario cuando el id_token no trae el correo; `sub` debe coincidir (§5.3.2)."""
    if not metadata.get("userinfo_endpoint") or not access_token:
        return {}
    info = get_json(
        metadata["userinfo_endpoint"],
        what=_("los datos del usuario"),
        headers={"Authorization": f"Bearer {access_token}"},
    )
    if info.get("sub") != subject:
        raise SsoError(_("Los datos del usuario no corresponden al id_token."))
    return info
