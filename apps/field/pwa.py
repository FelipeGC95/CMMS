"""Piezas de la PWA de terreno (ADR-011): manifest, precarga del Service Worker y su versión.

El Service Worker solo guarda la estructura de la aplicación (sin datos de la organización).
Su versión cambia con cada despliegue: en producción, los nombres con hash de los estáticos
más `RENDER_GIT_COMMIT`; con estáticos sin hash (desarrollo y tests), el contenido de cada
archivo. Un Service Worker con otra versión instala la precarga nueva y borra la anterior.
"""

import hashlib
import json
from functools import lru_cache

from django.conf import settings
from django.contrib.staticfiles import finders
from django.contrib.staticfiles.storage import ManifestFilesMixin, staticfiles_storage
from django.urls import reverse
from django.utils.translation import gettext as _

# Colores del manifest (claros): barra del sistema y pantalla de inicio. Un test los compara
# con static/css/tokens.css (--color-nav-bg y --color-bg).
THEME_COLOR = "#0B2E4F"
BACKGROUND_COLOR = "#F4F7FA"

ICONS = [
    ("brand/galbust-app-icon-192.png", "192x192", "any"),
    ("brand/galbust-app-icon-512.png", "512x512", "any"),
    ("brand/galbust-app-icon-maskable-512.png", "512x512", "maskable"),
]

# Módulos ES de la vista (static/field/). app.js es la entrada.
FIELD_SCRIPTS = [
    "field/app.js",
    "field/api.js",
    "field/db.js",
    "field/forms.js",
    "field/local.js",
    "field/photos.js",
    "field/queue.js",
    "field/sync.js",
    "field/ui.js",
]

# Estáticos que la vista necesita sin conexión (Cache Storage, caché primero).
SHELL_STATIC = [
    *FIELD_SCRIPTS,
    "css/tokens.css",
    "css/components.css",
    "js/theme.js",
    "js/app.js",
    "js/signature-pad.js",
    "brand/galbust-isotipo-blanco.svg",
    "brand/galbust-favicon.ico",
    "fonts/poppins-latin-400-normal.woff2",
    "fonts/poppins-latin-500-normal.woff2",
    "fonts/poppins-latin-600-normal.woff2",
    "fonts/inter-latin-400-normal.woff2",
    "fonts/inter-latin-500-normal.woff2",
    "fonts/inter-latin-600-normal.woff2",
    "fonts/jetbrains-mono-latin-400-normal.woff2",
    *(path for path, _size, _purpose in ICONS),
]


def manifest() -> dict:
    return {
        "name": _("GALBUST · Terreno"),
        "short_name": "GALBUST",
        "description": _("Órdenes de trabajo sin conexión para técnicos."),
        "lang": "es-CL",
        "dir": "ltr",
        "start_url": reverse("field_shell"),
        "scope": reverse("field_shell"),
        "display": "standalone",
        "orientation": "portrait",
        "theme_color": THEME_COLOR,
        "background_color": BACKGROUND_COLOR,
        "icons": [
            {"src": staticfiles_storage.url(path), "sizes": sizes, "type": "image/png", "purpose": purpose}
            for path, sizes, purpose in ICONS
        ],
    }


def precache_urls() -> list[str]:
    """URL que el Service Worker descarga al instalarse: la página base, el manifest y los estáticos."""
    return [
        reverse("field_shell"),
        reverse("field_manifest"),
        *(staticfiles_storage.url(path) for path in SHELL_STATIC),
    ]


def _hashed_names() -> bool:
    return isinstance(staticfiles_storage, ManifestFilesMixin)


@lru_cache(maxsize=4)
def _deployed_version(urls: tuple[str, ...], release: str) -> str:
    return hashlib.sha1(json.dumps([urls, release]).encode()).hexdigest()[:16]


def _content_version(urls: tuple[str, ...], release: str) -> str:
    digest = hashlib.sha1(json.dumps([urls, release]).encode())
    for path in SHELL_STATIC:
        found = finders.find(path)
        if found:
            with open(found, "rb") as handle:
                digest.update(handle.read())
    return digest.hexdigest()[:16]


def cache_version() -> str:
    """Versión del Service Worker. En producción se calcula una vez por proceso."""
    urls = tuple(precache_urls())
    release = getattr(settings, "APP_RELEASE", "")
    if _hashed_names():
        return _deployed_version(urls, release)
    return _content_version(urls, release)
