from django.contrib.auth.decorators import login_required
from django.http import HttpResponse
from django.shortcuts import render
from django.views.decorators.cache import cache_control

from apps.assets.models import Asset
from apps.core.decorators import membership_required
from apps.work.models import WorkOrder

from .tokens import color_tokens


@login_required
def design_system_view(request):
    """Catálogo vivo de tokens y componentes base (docs/06 §5)."""
    tokens = color_tokens()
    return render(
        request,
        "ui/design_system.html",
        {
            "colors": [(name, tokens["light"][name], tokens["dark"][name]) for name in tokens["light"]],
            "asset_statuses": [Asset(status=s) for s in Asset.Status.values],
            "work_order_statuses": [WorkOrder(status=s) for s in WorkOrder.Status.values],
            "priorities": [WorkOrder(priority=p) for p in WorkOrder.Priority.values],
        },
    )


@cache_control(max_age=300)
def design_system_css_view(request):
    """Muestras de color: una regla por token (sin estilos en línea, compatible con CSP)."""
    rules = [
        f'.swatch[data-token="{name}"] {{ background: var({name}); }}' for name in color_tokens()["light"]
    ]
    return HttpResponse("\n".join(rules), content_type="text/css")


@membership_required
def menu_view(request):
    """Menú "Más" de la navegación móvil: todo destino del menú de escritorio (ADR-017)."""
    return render(request, "ui/menu.html")
