from django.apps import apps
from django.db import connection
from django.http import HttpResponse


def healthz(request):
    """Vida: el proceso responde (no toca la base)."""
    return HttpResponse("ok", content_type="text/plain")


def readyz(request):
    """Preparado (NF-15): la base responde y, si la cola está instalada, sus tablas existen."""
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            if apps.is_installed("procrastinate.contrib.django"):
                cursor.execute("SELECT 1 FROM procrastinate_jobs LIMIT 1")
    except Exception:  # cualquier falla de la base es "no preparado"
        return HttpResponse("unavailable", status=503, content_type="text/plain")
    return HttpResponse("ok", content_type="text/plain")
