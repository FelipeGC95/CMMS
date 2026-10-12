import datetime
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
