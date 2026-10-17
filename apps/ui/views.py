import datetime
import uuid
from decimal import Decimal

from django.contrib.auth.decorators import login_required
from django.http import HttpResponse
from django.shortcuts import render
from django.views.decorators.cache import cache_control

from apps.assets.models import Asset, Meter, MeterKind
from apps.condition.charts import TrendPoint
from apps.condition.models import ConditionAlert, ConditionRule
from apps.condition.selectors import VariableCondition, VariableStatus
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
            "alert_statuses": [ConditionAlert(status=s) for s in ConditionAlert.Status.values],
            "variable_sample": _variable_sample(),
            "assistant_sample": _assistant_sample(),
            "variable_statuses": [
                VariableCondition(meter=Meter(), status=s)
                for s in (
                    VariableStatus.NORMAL,
                    VariableStatus.ALARM,
                    VariableStatus.NO_RULES,
                    VariableStatus.NO_READINGS,
                    VariableStatus.INACTIVE,
                )
            ],
        },
    )


def _variable_sample() -> VariableCondition:
    """Una variable de ejemplo con tendencia, umbral y una lectura fuera de rango (CBM-07)."""
    until = datetime.datetime(2026, 10, 12, 8, 0, tzinfo=datetime.UTC)
    since = until - datetime.timedelta(days=90)
    values = ["68", "70", "69.5", "72", "71", "74", "76", "75", "79", "84", "82"]
    rule = ConditionRule(operator=ConditionRule.Operator.GT, threshold=Decimal("80"))
    points = [
        TrendPoint(
            at=since + datetime.timedelta(days=8 * i + 4), value=Decimal(v), out_of_range=Decimal(v) > 80
        )
        for i, v in enumerate(values)
    ]
    meter = Meter(
        kind=MeterKind.TEMPERATURE,
        name="Rodamiento",
        unit="°C",
        current_value=Decimal("82"),
        current_read_at=points[-1].at,
    )
    return VariableCondition(
        meter=meter,
        status=VariableStatus.ALARM,
        rules=[rule],
        points=points,
        active_alerts=1,
        since=since,
        until=until,
    )


def _assistant_sample() -> dict:
    """Conversación de ejemplo (sin guardar): respuesta con fuentes, general, pendiente y error."""
    from apps.assistant.models import AssistantConversation, AssistantMessage
    from apps.assistant.views import with_display

    ids = [str(uuid.UUID(int=n)) for n in range(1, 4)]
    sources = [
        {
            "id": "OT-C-00012",
            "type": "work_order",
            "label": "OT-C-00012 · Cambio de rodamiento",
            "work_order_id": ids[0],
        },
        {
            "id": f"MAN:{ids[1]}:14",
            "type": "manual",
            "label": "Manual bomba.pdf, p. 14",
            "attachment_id": ids[1],
            "page": 14,
        },
        {
            "id": f"MED:{ids[2]}",
            "type": "meter",
            "label": "Temperatura rodamiento (°C)",
            "meter_id": ids[2],
            "asset_id": ids[2],
        },
    ]
    text = (
        "Revisa primero el rodamiento del lado acople: en marzo tuvo el mismo ruido [OT-C-00012].\n\n"
        f"El manual pide reengrasar cada 2.000 h [MAN:{ids[1]}:14] y la temperatura sube "
        f"[MED:{ids[2]}].\n\n"
        "Si el ruido sigue con el equipo en vacío, puede ser desalineación."
    )
    Message = AssistantMessage
    turns = [
        Message(role="user", seq=1, text="El motor hace ruido al partir y se calienta."),
        Message(role="assistant", seq=2, status="done", text=text, sources=sources),
        Message(role="user", seq=3, text="¿Cómo reviso la alineación?"),
        Message(role="assistant", seq=4, status="error", error_code="rate_limited"),
        Message(role="assistant", seq=5, status="pending"),
    ]
    return {
        "conversation": AssistantConversation(title="Ruido al partir", status="open", helpful="partial"),
        "turns": with_display(turns),
    }


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
