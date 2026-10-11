"""Vista de terreno sin conexión (X-06, ADR-011).

`/field/` es solo la estructura (se guarda en el Service Worker): no lleva ningún dato de la
organización ni de la persona. Los datos llegan por `/field/api/` y quedan en IndexedDB.
"""

import datetime
import json
from pathlib import Path

from django.conf import settings
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import render
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from django.utils.http import urlencode
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET

from apps.core.decorators import membership_required
from apps.work.models import FailureEvent, WorkOrder, WorkOrderChecklistItem

from . import pwa

DEMO_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "demo.json"
DATE_KEYS = {"scheduled_for", "due_on"}
DATETIME_KEYS = {"server_time", "generated_at", "updated_at", "answered_at", "created_at"}


@require_GET
@membership_required
@ensure_csrf_cookie  # el JS lee la cookie csrftoken y la envía en X-CSRFToken
def shell_view(request):
    shell = reverse("field_shell")
    return render(
        request,
        "field/shell.html",
        {
            "theme_color": pwa.THEME_COLOR,
            "result_choices": WorkOrderChecklistItem.Result.choices,
            "priority_choices": WorkOrder.Priority.choices,
            "impact_choices": FailureEvent.Impact.choices,
            "api_url": reverse("field_api_bootstrap").removesuffix("bootstrap/"),
            "login_url": f"{reverse('account_login')}?{urlencode({'next': shell})}",
        },
    )


@require_GET
def sw_view(request):
    """Service Worker con alcance /field/. Sin sesión: el navegador lo pide para actualizarse."""
    body = render_to_string(
        "field/sw.js",
        {
            "cache_version": pwa.cache_version(),
            "precache": json.dumps(pwa.precache_urls()),
            "shell_url": json.dumps(pwa.precache_urls()[0]),
        },
    )
    response = HttpResponse(body, content_type="application/javascript; charset=utf-8")
    response["Cache-Control"] = "no-cache"
    return response


@require_GET
def manifest_view(request):
    response = JsonResponse(
        pwa.manifest(), content_type="application/manifest+json", json_dumps_params={"ensure_ascii": False}
    )
    response["Cache-Control"] = "no-cache"
    return response


def _shift(value, days: int):
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if key in DATE_KEYS and item:
                out[key] = (datetime.date.fromisoformat(item) + datetime.timedelta(days=days)).isoformat()
            elif key in DATETIME_KEYS and item:
                moved = datetime.datetime.fromisoformat(item.replace("Z", "+00:00"))
                out[key] = (moved + datetime.timedelta(days=days)).isoformat().replace("+00:00", "Z")
            else:
                out[key] = _shift(item, days)
        return out
    if isinstance(value, list):
        return [_shift(item, days) for item in value]
    return value


def demo_payload(today: datetime.date) -> dict:
    """Fixture con el contrato de §2 (`bootstrap` + `snapshot`) movido para que "hoy" sea `today`."""
    data = json.loads(DEMO_FIXTURE.read_text(encoding="utf-8"))
    base = datetime.datetime.fromisoformat(data["bootstrap"]["server_time"].replace("Z", "+00:00")).date()
    return _shift(data, (today - base).days)


@require_GET
@membership_required
def demo_data_view(request):
    """Fixture del contrato (`bootstrap` + `snapshot`) para tests y demostraciones (solo con DEBUG).

    La vista ya no lo carga: descarga de `/field/api/`."""
    if not settings.DEBUG:
        raise Http404
    return JsonResponse(demo_payload(timezone.localdate()), json_dumps_params={"ensure_ascii": False})
