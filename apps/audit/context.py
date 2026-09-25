"""Origen de la petición en curso, fijado por `AuditContextMiddleware`."""

from contextvars import ContextVar
from dataclasses import dataclass

from django.conf import settings

_request_origin = ContextVar("audit_request_origin", default=None)


@dataclass(frozen=True)
class RequestOrigin:
    ip: str | None
    user_agent: str
    request_id: str = ""

    @classmethod
    def from_request(cls, request, *, request_id: str = ""):
        return cls(
            ip=client_ip(request.META),
            user_agent=request.META.get("HTTP_USER_AGENT", "")[:300],
            request_id=request_id,
        )


def client_ip(meta) -> str | None:
    """IP del cliente. Con N proxies de confianza se toma X-Forwarded-For[-N].

    Nunca el primer valor de X-Forwarded-For: lo escribe el cliente y se puede falsificar.
    """
    trusted = settings.AUDIT_TRUSTED_PROXIES
    if trusted > 0:
        forwarded = [ip.strip() for ip in meta.get("HTTP_X_FORWARDED_FOR", "").split(",") if ip.strip()]
        if len(forwarded) >= trusted:
            return forwarded[-trusted]
    return meta.get("REMOTE_ADDR") or None


def get_request_origin() -> RequestOrigin | None:
    return _request_origin.get()


def set_request_origin(origin: RequestOrigin | None):
    return _request_origin.set(origin)


def reset_request_origin(token) -> None:
    _request_origin.reset(token)
