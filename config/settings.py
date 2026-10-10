"""Settings de GALBUST CMMS.

Un solo archivo configurado por variables de entorno (ver docs/04_arquitectura.md).
"""

import os
from pathlib import Path

import dj_database_url

from config import infra

BASE_DIR = Path(__file__).resolve().parent.parent


def env_bool(name: str, default: bool) -> bool:
    return os.environ.get(name, "1" if default else "0").lower() in {"1", "true", "yes"}


SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "dev-insecure-key-only-for-local-use")
DEBUG = env_bool("DJANGO_DEBUG", True)
ALLOWED_HOSTS = ["*"] if DEBUG else [h for h in os.environ.get("DJANGO_ALLOWED_HOSTS", "").split(",") if h]
# Render publica el dominio del servicio en esta variable.
if render_host := os.environ.get("RENDER_EXTERNAL_HOSTNAME"):
    ALLOWED_HOSTS.append(render_host)
CSRF_TRUSTED_ORIGINS = [f"https://{h}" for h in ALLOWED_HOSTS if h != "*"]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "allauth",
    "allauth.account",
    "allauth.mfa",
    "rest_framework",
    "drf_spectacular",
    "drf_spectacular_sidecar",
    "apps.core",
    "apps.organizations",
    "apps.accounts",
    "apps.assets",
    "apps.maintenance",
    "apps.attachments",
    "apps.work",
    "apps.inventory",
    "apps.reporting",
    "apps.audit",
    "apps.notifications",
    "apps.api",
    "apps.ui",
    "apps.onboarding",
    "apps.field",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "apps.core.middleware.ContentSecurityPolicyMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.locale.LocaleMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "apps.audit.middleware.AuditContextMiddleware",
    "allauth.account.middleware.AccountMiddleware",
    "apps.core.middleware.TenantMiddleware",
    "apps.accounts.mfa.MFARequiredMiddleware",
    "apps.organizations.modules.PlanModuleMiddleware",
    "apps.core.middleware.PrivateCacheMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "apps.core.context_processors.tenant",
            ],
        },
    },
]

# PostgreSQL vía DATABASE_URL (CI, staging, producción); SQLite solo para la demo local.
DATABASES = {
    "default": dj_database_url.config(
        default=f"sqlite:///{BASE_DIR / 'db.sqlite3'}",
        conn_max_age=60,
        conn_health_checks=True,
    )
}

# Cola de tareas Procrastinate (ADR-007): solo sobre PostgreSQL (no soporta SQLite).
# El esquema se crea con `migrate`; nada corre hasta levantar el worker
# (`manage.py procrastinate worker`), así que en la demo queda inactiva.
TASK_QUEUE_ENABLED = DATABASES["default"]["ENGINE"] == "django.db.backends.postgresql"
if TASK_QUEUE_ENABLED:
    INSTALLED_APPS.insert(INSTALLED_APPS.index("apps.core"), "procrastinate.contrib.django")
# Texto de manuales PDF (AST-06 CA3): con DOCUMENT_TEXT_USE_QUEUE=1 lo extrae el worker; si no, al subir.
DOCUMENT_TEXT_USE_QUEUE = env_bool("DOCUMENT_TEXT_USE_QUEUE", False) and TASK_QUEUE_ENABLED
# Exportación completa (X-03): con DATA_EXPORT_USE_QUEUE=1 el ZIP lo arma el worker; si no, al pedirlo.
DATA_EXPORT_USE_QUEUE = env_bool("DATA_EXPORT_USE_QUEUE", False) and TASK_QUEUE_ENABLED
DATA_EXPORT_TTL_DAYS = 7
# Borrado de organización (X-04): días de gracia antes de la purga.
ORGANIZATION_DELETION_GRACE_DAYS = 30

AUTH_USER_MODEL = "accounts.User"
AUTHENTICATION_BACKENDS = [
    "django.contrib.auth.backends.ModelBackend",
    "allauth.account.auth_backends.AuthenticationBackend",
]
# Argon2 primero; las claves PBKDF2 existentes se migran solas al iniciar sesión.
PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.Argon2PasswordHasher",
    "django.contrib.auth.hashers.PBKDF2PasswordHasher",
]
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
        "OPTIONS": {"min_length": 10},
    },
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]
LOGIN_URL = "account_login"
LOGIN_REDIRECT_URL = "dashboard"
LOGOUT_REDIRECT_URL = "account_login"

# django-allauth (ADR-006): login por correo, sin nombre de usuario, correo verificado.
ACCOUNT_ADAPTER = "apps.accounts.adapter.AccountAdapter"
ACCOUNT_USER_MODEL_USERNAME_FIELD = None
ACCOUNT_LOGIN_METHODS = {"email"}
ACCOUNT_SIGNUP_FIELDS = ["email*", "password1*"]
ACCOUNT_UNIQUE_EMAIL = True
ACCOUNT_CHANGE_EMAIL = True
ACCOUNT_EMAIL_VERIFICATION = os.environ.get("ACCOUNT_EMAIL_VERIFICATION", "mandatory")
ACCOUNT_LOGIN_ON_EMAIL_CONFIRMATION = True
ACCOUNT_LOGIN_ON_PASSWORD_RESET = True
ACCOUNT_EMAIL_SUBJECT_PREFIX = "[GALBUST CMMS] "
ACCOUNT_DEFAULT_HTTP_PROTOCOL = "http" if DEBUG else "https"
# Verificación en dos pasos (ORG-08, ADR-022): app de autenticación (TOTP) y códigos de
# recuperación. Quién debe usarla lo decide cada organización (`mfa_policy`);
# MFA_ENFORCE=0 apaga la obligación (solo para tests que no tratan de MFA).
# Se acepta el código del intervalo vecino (±30 s, RFC 6238 §5.2): teléfonos de terreno con
# el reloj algo desfasado y códigos escritos justo al cambiar el intervalo.
MFA_TOTP_TOLERANCE = 1
MFA_ADAPTER = "apps.accounts.adapter.MFAAdapter"
MFA_SUPPORTED_TYPES = ["totp", "recovery_codes"]
MFA_ENFORCE = env_bool("MFA_ENFORCE", True)
# Autoregistro con prueba gratuita (ORG-02): límite por IP con el rate limit de allauth
# (sobre el caché en base de datos) y segundos mínimos entre mostrar y enviar el formulario.
ACCOUNT_RATE_LIMITS = {"signup": "5/m/ip,20/d/ip"}
ALLAUTH_TRUSTED_PROXY_COUNT = int(os.environ.get("AUDIT_TRUSTED_PROXIES", "0"))
SIGNUP_MIN_SECONDS = int(os.environ.get("SIGNUP_MIN_SECONDS", "3"))
# Límites de intentos por defecto de allauth (login, reinicio de clave) sobre el caché.
# Caché en la base de datos: compartido entre workers de gunicorn (LocMem es por proceso).
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.db.DatabaseCache",
        "LOCATION": "django_cache",
    }
}

# API REST (X-05, ADR-005): solo tokens de organización (sin sesión: no hay CSRF que
# cuidar), alcance por token, límite de peticiones por token y errores con forma única.
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": ["apps.api.framework.OrganizationTokenAuthentication"],
    "DEFAULT_PERMISSION_CLASSES": [
        "apps.api.framework.HasTokenScope",
        # Módulos por plan (ORG-10 CA2): `api` y el módulo del endpoint.
        "apps.organizations.modules.PlanIncludesModule",
    ],
    "DEFAULT_THROTTLE_CLASSES": ["apps.api.framework.TokenRateThrottle"],
    "DEFAULT_THROTTLE_RATES": {
        "api_token": os.environ.get("API_RATE_LIMIT", "600/min"),
        # API de terreno (ADR-011): por persona; una sincronización envía un cambio por petición.
        "field_user": os.environ.get("FIELD_API_RATE_LIMIT", "600/min"),
    },
    "DEFAULT_PAGINATION_CLASS": "apps.api.framework.Pagination",
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"],
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "EXCEPTION_HANDLER": "apps.api.exceptions.exception_handler",
    "UNAUTHENTICATED_USER": None,
}
SPECTACULAR_SETTINGS = {
    "TITLE": "GALBUST CMMS API",
    "DESCRIPTION": "API REST v1: activos, medidores y lecturas, órdenes de trabajo y solicitudes. "
    "Autenticación: `Authorization: Bearer <token>` (tokens en Más → Tokens de API).",
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
    "SERVE_PERMISSIONS": ["rest_framework.permissions.AllowAny"],
    "SERVE_AUTHENTICATION": [],
    "SWAGGER_UI_DIST": "SIDECAR",
    "SWAGGER_UI_FAVICON_HREF": "SIDECAR",
    "COMPONENT_SPLIT_REQUEST": True,
    # Fijo: con /field/api/ el prefijo común deducido cambiaría los operationId y etiquetas de v1.
    "SCHEMA_PATH_PREFIX": r"^/api",
    "ENUM_NAME_OVERRIDES": {
        "AssetStatusEnum": "apps.assets.models.Asset.Status",
        "WorkOrderStatusEnum": "apps.work.models.WorkOrder.Status",
        "WorkRequestStatusEnum": "apps.work.models.WorkRequest.Status",
        "ChecklistItemResultEnum": "apps.work.models.WorkOrderChecklistItem.Result",
    },
}

# Auditoría (X-01): cantidad de proxies de confianza delante de la app. Con N > 0
# la IP del cliente es X-Forwarded-For[-N]; con 0 se usa REMOTE_ADDR.
AUDIT_TRUSTED_PROXIES = int(os.environ.get("AUDIT_TRUSTED_PROXIES", "0"))

# Correo: consola en local; en producción SMTP del proveedor (Postmark o Brevo,
# docs/07_operacion.md). Con EMAIL_HOST definido se usa SMTP salvo que EMAIL_BACKEND diga otra cosa.
EMAIL_HOST = os.environ.get("EMAIL_HOST", "localhost")
EMAIL_BACKEND = os.environ.get(
    "EMAIL_BACKEND",
    "django.core.mail.backends.smtp.EmailBackend"
    if os.environ.get("EMAIL_HOST")
    else "django.core.mail.backends.console.EmailBackend",
)
EMAIL_PORT = int(os.environ.get("EMAIL_PORT", "587"))
EMAIL_HOST_USER = os.environ.get("EMAIL_HOST_USER", "")
EMAIL_HOST_PASSWORD = os.environ.get("EMAIL_HOST_PASSWORD", "")
EMAIL_USE_TLS = env_bool("EMAIL_USE_TLS", True)
EMAIL_TIMEOUT = int(os.environ.get("EMAIL_TIMEOUT", "10"))
DEFAULT_FROM_EMAIL = os.environ.get("DEFAULT_FROM_EMAIL", "GALBUST CMMS <no-reply@galbust.cl>")
SERVER_EMAIL = os.environ.get("SERVER_EMAIL", DEFAULT_FROM_EMAIL)
# EMAIL_USE_QUEUE=1: los correos se envían desde el worker (reintentos, sin bloquear la
# petición). Apagado por defecto: envío sincrónico como siempre.
if env_bool("EMAIL_USE_QUEUE", False) and TASK_QUEUE_ENABLED:
    EMAIL_DELIVERY_BACKEND = EMAIL_BACKEND
    EMAIL_BACKEND = "apps.core.mail.QueuedEmailBackend"
# URL pública para los enlaces de los correos de notificación (X-02), que también
# salen desde tareas y comandos sin petición.
SITE_URL = os.environ.get("SITE_URL", f"https://{render_host}" if render_host else "http://localhost:8000")

# Idioma fuente de la interfaz: español de Chile.
LANGUAGE_CODE = "es-cl"
LANGUAGES = [("es", "Español")]
LOCALE_PATHS = [BASE_DIR / "locale"]
USE_I18N = True

# Todo se guarda en UTC; el middleware activa la zona de la organización.
TIME_ZONE = "UTC"
USE_TZ = True

STATIC_URL = "static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"
# Archivos subidos: disco local, o Cloudflare R2 con R2_BUCKET, R2_ENDPOINT_URL,
# R2_ACCESS_KEY_ID y R2_SECRET_ACCESS_KEY (ADR-014). En R2 las descargas redirigen a
# una URL firmada de 5 minutos tras verificar permisos (apps.core.files).
STORAGES = {
    "default": infra.default_storage_config(os.environ),
    "staticfiles": {
        "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"
        if DEBUG
        # Hash en el nombre, compresión y `import` de módulos ES reescritos (config/storage.py).
        else "config.storage.ModuleManifestStaticFilesStorage"
    },
}
# Versión desplegada: invalida la caché de la vista de terreno (/field/sw.js, ADR-011).
APP_RELEASE = os.environ.get("RENDER_GIT_COMMIT", "")

# Solo con almacenamiento local (sin R2).
MEDIA_ROOT = Path(os.environ.get("MEDIA_ROOT", BASE_DIR / "media"))
MEDIA_URL = "media/"  # No se sirve públicamente: los archivos pasan por files.views.
MAX_UPLOAD_BYTES = 20 * 1024 * 1024  # AST-06 CA1

# Respaldo lógico diario (NF-09, docs/07_operacion.md): `backup_database` sube a
# `backups/` del bucket R2 o, sin R2, escribe en BACKUP_DIR. La tarea diaria del worker
# solo respalda con BACKUPS_ENABLED=1.
BACKUPS_ENABLED = env_bool("BACKUPS_ENABLED", False)
BACKUP_DIR = Path(os.environ.get("BACKUP_DIR", BASE_DIR / "backups"))
BACKUP_RETENTION_DAYS = int(os.environ.get("BACKUP_RETENTION_DAYS", "30"))

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# CSP estricta (ADR-023, docs/04 §5): sin scripts ni estilos en línea, sin marcos.
CONTENT_SECURITY_POLICY = infra.content_security_policy(os.environ)

SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
if not DEBUG:
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SECURE_SSL_REDIRECT = True
    SECURE_REDIRECT_EXEMPT = [r"^healthz$", r"^readyz$"]  # el health check de Render llega por HTTP interno
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_HSTS_SECONDS = 31536000
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_CONTENT_TYPE_NOSNIFF = True
    SECURE_REFERRER_POLICY = "same-origin"
    # HSTS preload se activa cuando el dominio definitivo esté estable (docs/04 §5).
    SILENCED_SYSTEM_CHECKS = ["security.W021"]

# Logs a stdout; LOG_FORMAT=json los emite estructurados con el id de la petición (NF-15).
LOGGING = infra.logging_config(os.environ)
if os.environ.get("LOG_FORMAT", "").lower() == "json":
    MIDDLEWARE.insert(0, "apps.core.logs.RequestIdMiddleware")

# Sentry solo si hay SENTRY_DSN; sin datos personales (send_default_pii=False).
if sentry := infra.sentry_options(os.environ):
    import sentry_sdk

    sentry_sdk.init(**sentry)
