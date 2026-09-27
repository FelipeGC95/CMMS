"""Configuración de infraestructura derivada de variables de entorno.

Funciones puras (reciben el entorno como diccionario) para poder probar cada
interruptor sin recargar `settings`. Todo lo de producción queda inactivo
mientras no se definan sus variables (ver docs/07_operacion.md).
"""

from collections.abc import Mapping
from urllib.parse import urlsplit

R2_REQUIRED_VARS = ("R2_BUCKET", "R2_ENDPOINT_URL", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY")
# ADR-014 / NF-06: las descargas son URLs firmadas de corta duración.
SIGNED_URL_SECONDS = 300


def env_flag(env: Mapping[str, str], name: str, default: bool = False) -> bool:
    return env.get(name, "1" if default else "0").lower() in {"1", "true", "yes"}


def r2_enabled(env: Mapping[str, str]) -> bool:
    """R2 se activa solo con las cuatro variables definidas; si falta alguna, disco local."""
    return all(env.get(name) for name in R2_REQUIRED_VARS)


def content_security_policy(env: Mapping[str, str]) -> str:
    """CSP estricta (ADR-022): todo desde el propio dominio, sin scripts ni estilos en línea.

    Las imágenes admiten `data:` (QR de la verificación en dos pasos) y `blob:` (vista
    previa de fotos). Con R2, las URL firmadas de descarga llegan por redirección desde
    el host del endpoint (o un subdominio con el bucket): se agregan a `img-src`.
    `CSP_EXTRA_IMG_SRC` (separado por espacios) suma otros orígenes si hiciera falta.
    """
    img_src = ["'self'", "data:", "blob:"]
    endpoint = env.get("R2_ENDPOINT_URL", "") if r2_enabled(env) else ""
    if endpoint:
        host = urlsplit(endpoint).netloc
        img_src += [f"https://{host}", f"https://*.{host}"]
    img_src += env.get("CSP_EXTRA_IMG_SRC", "").split()
    directives = {
        "default-src": ["'self'"],
        "script-src": ["'self'"],
        "style-src": ["'self'"],
        "img-src": img_src,
        "font-src": ["'self'"],
        "connect-src": ["'self'"],
        "object-src": ["'none'"],
        "base-uri": ["'self'"],
        "form-action": ["'self'"],
        "frame-ancestors": ["'none'"],
    }
    return "; ".join(f"{name} {' '.join(values)}" for name, values in directives.items())


def default_storage_config(env: Mapping[str, str]) -> dict:
    """Backend de archivos subidos: Cloudflare R2 (S3) con credenciales, o disco local."""
    if not r2_enabled(env):
        return {"BACKEND": "django.core.files.storage.FileSystemStorage"}
    return {
        "BACKEND": "storages.backends.s3.S3Storage",
        "OPTIONS": {
            "bucket_name": env["R2_BUCKET"],
            "endpoint_url": env["R2_ENDPOINT_URL"],
            "access_key": env["R2_ACCESS_KEY_ID"],
            "secret_key": env["R2_SECRET_ACCESS_KEY"],
            "region_name": "auto",
            "signature_version": "s3v4",
            # Bucket privado: nada público, todo por URL firmada que vence.
            "default_acl": None,
            "querystring_auth": True,
            "querystring_expire": SIGNED_URL_SECONDS,
            "file_overwrite": False,
        },
    }


def logging_config(env: Mapping[str, str]) -> dict:
    """Logs a stdout; con LOG_FORMAT=json, una línea JSON por evento con request_id."""
    json_format = env.get("LOG_FORMAT", "").lower() == "json"
    handler = {"class": "logging.StreamHandler"}
    config = {
        "version": 1,
        "disable_existing_loggers": False,
        "handlers": {"console": handler},
        "root": {"handlers": ["console"], "level": env.get("LOG_LEVEL", "INFO")},
        # Procrastinate anuncia cada tarea registrada en cada comando; solo avisos.
        "loggers": {"procrastinate": {"level": env.get("PROCRASTINATE_LOG_LEVEL", "WARNING")}},
    }
    if json_format:
        config["formatters"] = {"json": {"()": "apps.core.logs.JsonFormatter"}}
        config["filters"] = {"request_id": {"()": "apps.core.logs.RequestIdFilter"}}
        handler["formatter"] = "json"
        handler["filters"] = ["request_id"]
    return config


def sentry_options(env: Mapping[str, str]) -> dict | None:
    """Opciones de `sentry_sdk.init`, o None si no hay SENTRY_DSN (Sentry apagado)."""
    dsn = env.get("SENTRY_DSN")
    if not dsn:
        return None
    return {
        "dsn": dsn,
        # Sin datos personales (correos, IP, cookies) en los eventos.
        "send_default_pii": False,
        "environment": env.get("SENTRY_ENVIRONMENT", "production"),
        "release": env.get("SENTRY_RELEASE") or env.get("RENDER_GIT_COMMIT") or None,
        "traces_sample_rate": float(env.get("SENTRY_TRACES_SAMPLE_RATE", "0")),
    }
