"""Atributos técnicos por clase (AST-01): formulario dinámico, conversión a JSON y presentación."""

import datetime
from decimal import Decimal, InvalidOperation

from django import forms
from django.utils.formats import date_format, number_format
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy

from .models import AttributeType

BOOL_CHOICES = [("", "—"), ("true", gettext_lazy("Sí")), ("false", gettext_lazy("No"))]


def field_name(attribute) -> str:
    return f"attr_{attribute.key}"


def build_form_fields(attributes) -> dict:
    """Campos de Django para los atributos activos de una clase."""
    fields = {}
    for attribute in attributes:
        label = f"{attribute.label} ({attribute.unit})" if attribute.unit else attribute.label
        common = {"label": label, "required": attribute.required}
        if attribute.data_type == AttributeType.NUMBER:
            # localize: acepta coma decimal (es-CL).
            field = forms.DecimalField(max_digits=16, decimal_places=4, localize=True, **common)
        elif attribute.data_type == AttributeType.DATE:
            field = forms.DateField(
                widget=forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"), **common
            )
        elif attribute.data_type == AttributeType.BOOL:
            field = forms.ChoiceField(choices=BOOL_CHOICES, **common)
        elif attribute.data_type == AttributeType.CHOICE:
            field = forms.ChoiceField(choices=[("", "—"), *[(c, c) for c in attribute.choices]], **common)
        else:
            field = forms.CharField(max_length=200, **common)
        fields[field_name(attribute)] = field
    return fields


def initial_values(attributes, stored: dict) -> dict:
    initial = {}
    for attribute in attributes:
        value = stored.get(attribute.key)
        if value is None:
            continue
        if attribute.data_type == AttributeType.BOOL:
            value = "true" if value else "false"
        initial[field_name(attribute)] = value
    return initial


def to_json(attribute, value):
    """Valor limpio del formulario → valor guardable en JSON. Lanza ValueError si no calza con el tipo."""
    if value in (None, ""):
        return None
    kind = attribute.data_type
    if kind == AttributeType.NUMBER:
        try:
            # Formato fijo: normalize() a secas guarda 30 como "3E+1".
            return f"{Decimal(str(value)).normalize():f}"
        except InvalidOperation as error:
            raise ValueError(_("%(label)s debe ser un número.") % {"label": attribute.label}) from error
    if kind == AttributeType.DATE:
        if isinstance(value, datetime.date):
            return value.isoformat()
        try:
            return datetime.date.fromisoformat(str(value)).isoformat()
        except ValueError as error:
            raise ValueError(_("%(label)s debe ser una fecha.") % {"label": attribute.label}) from error
    if kind == AttributeType.BOOL:
        if isinstance(value, bool):
            return value
        if value in ("true", "false"):
            return value == "true"
        raise ValueError(_("%(label)s debe ser Sí o No.") % {"label": attribute.label})
    if kind == AttributeType.CHOICE:
        if value not in attribute.choices:
            raise ValueError(_("%(label)s: opción no válida.") % {"label": attribute.label})
        return value
    return str(value).strip()[:200]


def display(attribute, value) -> str:
    """Texto para la ficha del activo."""
    if value in (None, ""):
        return "—"
    kind = attribute.data_type
    if kind == AttributeType.BOOL:
        return _("Sí") if value else _("No")
    if kind == AttributeType.NUMBER:
        text = number_format(Decimal(value), use_l10n=True)
        return f"{text} {attribute.unit}".strip()
    if kind == AttributeType.DATE:
        return date_format(datetime.date.fromisoformat(value), "d-m-Y")
    return f"{value} {attribute.unit}".strip() if attribute.unit else str(value)


def attribute_rows(asset, attributes) -> list[tuple]:
    """[(atributo, texto)] de los atributos activos de la clase del activo."""
    return [(attribute, display(attribute, asset.attributes.get(attribute.key))) for attribute in attributes]
