"""Formularios web de mantenimiento por condición (CBM-03, CBM-05).

Validan la entrada; las reglas (rangos, alcance, organización) están en los servicios.
"""

import uuid

from django import forms
from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

from apps.assets.models import VARIABLE_METER_KIND_CHOICES, Asset, AssetClass, Meter
from apps.organizations.models import Location
from apps.organizations.selectors import visible_locations

from .models import ConditionAlert, ConditionRule
from .selectors import alert_assets, variable_meters

Operator = ConditionRule.Operator
Action = ConditionRule.Action

STATUS_ACTIVE = "active"
STATUS_ALL = "all"
SCOPE_METER = "meter"
SCOPE_CLASS = "class"
RULE_FIELDS = (
    "name",
    "operator",
    "threshold",
    "min_value",
    "max_value",
    "consecutive",
    "action",
    "work_order_title",
    "work_order_priority",
    "work_order_instructions",
)


def apply_errors(form, error: ValidationError) -> None:
    """Muestra un error de servicio en su campo (o arriba del formulario si el campo no existe)."""
    if hasattr(error, "error_dict"):
        for field, errors in error.message_dict.items():
            form.add_error(field if field in form.fields else None, errors)
    else:
        form.add_error(None, error.messages)


def _uuid_or_none(value):
    try:
        return uuid.UUID(str(value)) if value else None
    except ValueError:
        return None


class AlertFilterForm(forms.Form):
    """Filtros de la lista de alertas (CBM-05 CA1); por defecto, las activas."""

    status = forms.ChoiceField(
        label=_("Estado"),
        required=False,
        choices=[
            (STATUS_ACTIVE, _("Activas (abiertas y reconocidas)")),
            *ConditionAlert.Status.choices,
            (STATUS_ALL, _("Todas")),
        ],
    )
    location = forms.ModelChoiceField(
        # `all_tenants`: al definir la clase no hay organización activa; se reemplaza en __init__.
        label=_("Sitio o ubicación"),
        queryset=Location.all_tenants.none(),
        required=False,
        empty_label=_("Todas"),
        help_text=_("Incluye las ubicaciones que contiene."),
    )
    asset = forms.ModelChoiceField(
        label=_("Activo"), queryset=Asset.all_tenants.none(), required=False, empty_label=_("Todos")
    )

    def __init__(self, *args, membership, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["location"].queryset = (
            visible_locations(membership=membership).filter(is_active=True).order_by("code")
        )
        self.fields["location"].label_from_instance = lambda loc: f"{loc.name} ({loc.code})"
        # Solo los activos con alertas (la lista no crece con el inventario de activos), más el
        # pedido por la URL (enlace desde la ficha del activo).
        requested = _uuid_or_none(self.data.get("asset")) if self.is_bound else None
        self.fields["asset"].queryset = alert_assets(membership=membership, include=requested)
        self.fields["asset"].label_from_instance = lambda asset: f"{asset.code} · {asset.name}"

    def filters(self) -> dict:
        """Filtros válidos para `alert_list`; un campo con error se ignora."""
        data = getattr(self, "cleaned_data", {}) if self.is_bound else {}
        data = {key: value for key, value in data.items() if key not in self.errors}
        status = data.get("status") or STATUS_ACTIVE
        return {
            "status": "" if status == STATUS_ALL else status,
            "location": data.get("location"),
            "asset": data.get("asset"),
        }

    @property
    def active_count(self) -> int:
        data = getattr(self, "cleaned_data", {})
        count = sum(1 for key in ("location", "asset") if data.get(key))
        return count + (1 if data.get("status") not in ("", STATUS_ACTIVE, None) else 0)


class DecimalText(forms.TextInput):
    """Número con coma o punto (el servicio lo normaliza); teclado numérico en el teléfono."""

    def __init__(self, attrs=None):
        super().__init__({"inputmode": "decimal", "autocomplete": "off", **(attrs or {})})


class ConditionRuleForm(forms.Form):
    """Alta y edición de una regla (CBM-03). Al editar no hay campos de alcance (no se cambia)."""

    scope = forms.ChoiceField(
        label=_("¿Dónde aplica?"),
        choices=[
            (SCOPE_METER, _("Una variable de un activo")),
            (SCOPE_CLASS, _("Todos los activos de una clase")),
        ],
        initial=SCOPE_METER,
        widget=forms.RadioSelect,
    )
    meter = forms.ModelChoiceField(
        label=_("Variable"),
        queryset=Meter.all_tenants.none(),
        required=False,
        empty_label=_("Elige la variable"),
        help_text=_("Variables de condición de los activos que ves. Se agregan en la ficha del activo."),
    )
    asset_class = forms.ModelChoiceField(
        label=_("Clase de activo"),
        queryset=AssetClass.all_tenants.none(),
        required=False,
        empty_label=_("Elige la clase"),
        help_text=_("Aplica también a los activos de la clase que se creen después."),
    )
    meter_kind = forms.ChoiceField(
        label=_("Tipo de variable"),
        required=False,
        choices=[("", _("Elige el tipo")), *VARIABLE_METER_KIND_CHOICES],
    )
    meter_name = forms.CharField(
        label=_("Nombre de la variable"),
        max_length=80,
        required=False,
        help_text=_(
            "Igual que en cada activo, p. ej. «Rodamiento». Vacío: la variable de ese tipo sin nombre."
        ),
    )
    operator = forms.ChoiceField(
        label=_("Condición"), choices=Operator.choices, initial=Operator.GT, widget=forms.RadioSelect
    )
    threshold = forms.CharField(
        label=_("Umbral"),
        required=False,
        widget=DecimalText,
        help_text=_(
            "En «Cambio por día mayor que», el cambio máximo por día respecto de la lectura anterior."
        ),
    )
    min_value = forms.CharField(label=_("Mínimo"), required=False, widget=DecimalText)
    max_value = forms.CharField(label=_("Máximo"), required=False, widget=DecimalText)
    consecutive = forms.IntegerField(
        label=_("Lecturas seguidas fuera de rango"),
        min_value=1,
        max_value=100,
        initial=1,
        help_text=_("1 alerta con la primera lectura; más filtra el ruido de los sensores."),
    )
    action = forms.ChoiceField(
        label=_("Qué hacer"),
        choices=[
            (Action.ALERT_WORK_ORDER, Action.ALERT_WORK_ORDER.label),
            (Action.ALERT, Action.ALERT.label),
        ],
        initial=Action.ALERT_WORK_ORDER,
        widget=forms.RadioSelect,
    )
    work_order_title = forms.CharField(
        label=_("Título de la OT"),
        max_length=200,
        required=False,
        help_text=_("Vacío: «Condición: <variable> en <activo>»."),
    )
    work_order_priority = forms.ChoiceField(
        label=_("Prioridad de la OT"),
        required=False,
        choices=[("", _("Según la criticidad del activo")), *ConditionRule.Priority.choices],
    )
    work_order_instructions = forms.CharField(
        label=_("Instrucciones de la OT"), required=False, widget=forms.Textarea(attrs={"rows": 3})
    )
    name = forms.CharField(
        label=_("Nombre de la regla"),
        max_length=120,
        required=False,
        help_text=_("Opcional. Vacío: se describe sola, p. ej. «Rodamiento · CMP-01 > 80»."),
    )

    SCOPE_FIELDS = ("scope", "meter", "asset_class", "meter_kind", "meter_name")

    def __init__(self, *args, membership, rule=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.rule = rule
        if rule is not None:
            for name in self.SCOPE_FIELDS:
                del self.fields[name]
            return
        # Solo variables visibles (alcance por sitio) y clases activas de la organización: un ID
        # ajeno no es una opción válida.
        self.fields["meter"].queryset = variable_meters(membership=membership)
        self.fields["meter"].label_from_instance = lambda meter: (
            f"{meter.asset.code} · {meter.name or meter.get_kind_display()} ({meter.unit})"
            if meter.unit
            else f"{meter.asset.code} · {meter.name or meter.get_kind_display()}"
        )
        self.fields["asset_class"].queryset = AssetClass.objects.filter(is_active=True).order_by("name")
        self.fields["asset_class"].label_from_instance = lambda cls: f"{cls.name} ({cls.code})"

    @classmethod
    def for_rule(cls, rule, *, membership, data=None):
        initial = {name: getattr(rule, name) for name in RULE_FIELDS}
        for name in ("threshold", "min_value", "max_value"):
            value = initial[name]
            initial[name] = "" if value is None else _plain(value)
        initial["work_order_priority"] = rule.work_order_priority or ""
        return cls(data, membership=membership, rule=rule, initial=initial)

    def clean(self):
        data = super().clean()
        if self.rule is None:
            if data.get("scope") == SCOPE_METER and not data.get("meter") and "meter" not in self.errors:
                self.add_error("meter", _("Elige la variable."))
            if (
                data.get("scope") == SCOPE_CLASS
                and not data.get("asset_class")
                and "asset_class" not in self.errors
            ):
                self.add_error("asset_class", _("Elige la clase de activo."))
        return data

    def service_kwargs(self) -> dict:
        """Argumentos para `create_rule` (con alcance) o `update_rule` (sin él)."""
        data = self.cleaned_data
        values = {name: data[name] for name in RULE_FIELDS}
        values["work_order_priority"] = data["work_order_priority"] or None
        if self.rule is not None:
            return values
        if data["scope"] == SCOPE_METER:
            return {**values, "meter": data["meter"]}
        return {
            **values,
            "asset_class": data["asset_class"],
            "meter_kind": data["meter_kind"],
            "meter_name": data["meter_name"],
        }


def _plain(value) -> str:
    """80.000 se muestra «80» y 2.500 «2.5» en el campo (sin ceros sobrantes)."""
    return format(value.normalize(), "f")
