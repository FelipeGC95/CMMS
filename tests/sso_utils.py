"""IdP OIDC falso para los tests de SSO (ORG-11): sin red, con JWT firmados de verdad.

`FakeIdP` responde descubrimiento, JWKS, token y userinfo reemplazando
`apps.sso.oidc.http_request`; `install_idps` enruta por prefijo de URL para tener varios
IdP a la vez (aislamiento). `lookup_txt` también se reemplaza: `dns` es un dict nombre → valores.
"""

import json
import time
from functools import cache
from urllib.parse import parse_qs, urlsplit

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from django.urls import reverse

from apps.core.tenancy import tenant_context
from apps.sso import oidc, services
from apps.sso.models import SsoDomain


@cache
def rsa_key(name="default"):
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


class FakeIdP:
    def __init__(self, slug, *, client_id="galbust-app", key_name="default"):
        self.slug = slug
        self.issuer = f"https://idp.example.com/{slug}"
        self.client_id = client_id
        self.key = rsa_key(key_name)
        self.kid = f"kid-{key_name}"
        self.claims = {}  # sobrescribe datos del id_token del próximo canje
        self.userinfo = None
        self.drop_email = False
        self.signing_key = None  # otra clave para simular una firma inválida
        self.algorithm = "RS256"
        self.last_code = None
        self.nonce = None
        self.token_requests = []

    @property
    def discovery_url(self):
        return self.issuer + oidc.DISCOVERY_SUFFIX

    def metadata(self):
        return {
            "issuer": self.issuer,
            "authorization_endpoint": f"{self.issuer}/authorize",
            "token_endpoint": f"{self.issuer}/token",
            "jwks_uri": f"{self.issuer}/keys",
            "userinfo_endpoint": f"{self.issuer}/userinfo",
            "response_types_supported": ["code"],
            "token_endpoint_auth_methods_supported": ["client_secret_post", "client_secret_basic"],
        }

    def jwks(self):
        data = jwt.algorithms.RSAAlgorithm.to_jwk(self.key.public_key(), as_dict=True)
        return {"keys": [{**data, "kid": self.kid, "use": "sig"}]}

    def id_token(self, email):
        now = int(time.time())
        claims = {
            "iss": self.issuer,
            "aud": self.client_id,
            "sub": f"sub-{email}",
            "iat": now,
            "exp": now + 300,
            "nonce": self.nonce,
            "email": email,
            "email_verified": True,
            "name": "Persona SSO",
            **self.claims,
        }
        if self.drop_email:
            claims.pop("email", None)
        claims = {key: value for key, value in claims.items() if value is not None}
        if self.algorithm == "HS256":
            return jwt.encode(
                claims, "secreto-del-cliente-123" * 2, algorithm="HS256", headers={"kid": self.kid}
            )
        key = self.signing_key or self.key
        return jwt.encode(claims, key, algorithm=self.algorithm, headers={"kid": self.kid})

    def handle(self, method, url, body):
        path = url[len(self.issuer) :]
        if method == "GET" and url == self.discovery_url:
            return 200, json.dumps(self.metadata()).encode()
        if method == "GET" and path == "/keys":
            return 200, json.dumps(self.jwks()).encode()
        if method == "POST" and path == "/token":
            form = {key: values[0] for key, values in parse_qs(body.decode()).items()}
            self.token_requests.append(form)
            email = form["code"].removeprefix("code-for:")
            return 200, json.dumps({"id_token": self.id_token(email), "access_token": "at-123"}).encode()
        if method == "GET" and path == "/userinfo":
            return 200, json.dumps(self.userinfo or {}).encode()
        return 404, b"{}"


def install_idps(monkeypatch, *idps):
    calls = []

    def fake_http_request(method, url, *, body=None, headers=None):
        calls.append((method, url))
        for idp in idps:
            if url.startswith(idp.issuer + "/"):
                return idp.handle(method, url, body)
        raise oidc.SsoError("sin red en los tests")

    monkeypatch.setattr(oidc, "http_request", fake_http_request)
    return calls


def install_dns(monkeypatch):
    records = {}
    monkeypatch.setattr(services, "lookup_txt", lambda name: records.get(name, []))
    return records


def configure(tenant, idp, *, domains=("empresa-a.cl",), verified=True, **options):
    """Configuración SSO activa del tenant con sus dominios (verificados por defecto)."""
    with tenant_context(tenant.org):
        configuration = services.save_configuration(
            membership=tenant.admin,
            discovery_url=idp.issuer,
            client_id=idp.client_id,
            client_secret="secreto-del-cliente-123",
            default_role=options.pop("default_role", "technician"),
            is_active=options.pop("is_active", True),
            **options,
        )
        for name in domains:
            domain = services.add_domain(membership=tenant.admin, domain=name)
            if verified:
                SsoDomain.objects.filter(pk=domain.pk).update(verified_at=domain.created_at)
    return configuration


def sso_round_trip(client, idp, email, *, state=None):
    """Pide el SSO con el correo, simula la vuelta del IdP y devuelve la respuesta del callback."""
    start = client.post(reverse("sso_login"), {"email": email})
    assert start.status_code == 302, start.content.decode()[:500]
    query = {key: values[0] for key, values in parse_qs(urlsplit(start["Location"]).query).items()}
    idp.nonce = query["nonce"]
    return client.get(
        reverse("sso_callback"), {"code": f"code-for:{email}", "state": state or query["state"]}
    )
