"""Formularios de planes preventivos (PM-01..PM-04)."""

from django import forms
from django.utils.translation import gettext_lazy as _

from apps.assets.models import USAGE_METER_KIND_CHOICES, AssetClass
from apps.organizations.models import Location
from apps.organizations.selectors import visible_locations

from .models import MAX_HORIZON_DAYS, CalendarUnit, ItemType, MaintenancePlan, PlanResource, PlanVersion


def _class_field(**kwargs):
    # `all_tenants`: al definir la clase no hay organización activa en el contexto.
    return forms.ModelChoiceField(queryset=AssetClass.all_tenants.none(), **kwargs)


def _location_label(location):
    return f"{location.name} ({location.code})"


class PlanFilterForm(forms.Form):
    q = forms.CharField(label=_("Buscar"), required=False, max_length=100, help_text=_("Código o nombre."))
    active = forms.ChoiceField(
        label=_("Estado"),
        required=False,
        choices=[("", _("Todos")), ("1", _("Activos")), ("0", _("Inactivos"))],
    )
    asset_class = _class_field(label=_("Clase de activo"), required=False, empty_label=_("Todas las clases"))
    trigger = forms.ChoiceField(
        label=_("Disparador"),
        required=False,
        choices=[
            ("", _("Todos")),
            ("calendar", _("Por calendario")),
            ("meter", _("Por medidor")),
            ("mixed", _("Mixto (lo que ocurra primero)")),
        ],
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["asset_class"].queryset = AssetClass.objects.order_by("name")


class PlanForm(forms.Form):
    """Datos del plan que no se versionan."""

    code = forms.CharField(label=_("Código"), max_length=30)
    name = forms.CharField(label=_("Nombre"), max_length=150)
    asset_class = _class_field(
        label=_("Clase de activo"),
        required=False,
        empty_label=_("Sin clase"),
        help_text=_("Los activos nuevos de esta clase reciben el plan automáticamente."),
    )
    is_active = forms.BooleanField(label=_("Plan activo"), required=False, initial=True)

    def __init__(self, *args, show_active=True, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["asset_class"].queryset = AssetClass.objects.filter(is_active=True).order_by("name")
        if not show_active:
            del self.fields["is_active"]


class TriggerForm(forms.Form):
    """Frecuencia y reglas de la versión (borrador)."""

    calendar_interval = forms.IntegerField(
        label=_("Cada cuánto (calendario)"),
        min_value=1,
        max_value=3650,
        required=False,
        help_text=_("Vacío si solo es por medidor."),
    )
    calendar_unit = forms.ChoiceField(
        label=_("Unidad"),
        choices=CalendarUnit.choices,
        initial=CalendarUnit.DAYS,
        required=False,
        help_text=_("Meses de calendario: mismo día del mes (o el último, si no existe)."),
    )
    meter_kind = forms.ChoiceField(
        label=_("Medidor"), required=False, choices=[("", _("Sin medidor")), *USAGE_METER_KIND_CHOICES]
    )
    meter_interval = forms.DecimalField(
        label=_("Cada cuántas unidades del medidor"),
        max_digits=14,
        decimal_places=3,
        min_value=0.001,
        required=False,
    )
    schedule_mode = forms.ChoiceField(
        label=_("Reprogramación"), choices=PlanVersion.ScheduleMode.choices, initial="floating"
    )
    tolerance_pct = forms.IntegerField(
        label=_("Tolerancia de cumplimiento (%)"), min_value=0, max_value=100, initial=10
    )
    estimated_minutes = forms.IntegerField(label=_("Duración estimada (min)"), min_value=1, initial=60)
    instructions = forms.CharField(
        label=_("Instrucciones"),
        required=False,
        widget=forms.Textarea(attrs={"rows": 3}),
        help_text=_("Se muestran en la OT preventiva."),
    )

    def clean(self):
        cleaned = super().clean()
        days, kind, interval = (
            cleaned.get("calendar_interval"),
            cleaned.get("meter_kind"),
            cleaned.get("meter_interval"),
        )
        if bool(kind) != bool(interval):
            raise forms.ValidationError(_("Para disparar por medidor indica el tipo y la frecuencia."))
        if not days and not kind and not self.errors:
            raise forms.ValidationError(_("Define una frecuencia por calendario, por medidor o ambas."))
        return cleaned

    def version_fields(self):
        data = self.cleaned_data
        return {
            "calendar_interval": data["calendar_interval"],
            "calendar_unit": data["calendar_unit"] or CalendarUnit.DAYS,
            "meter_kind": data["meter_kind"],
            "meter_interval": data["meter_interval"] if data["meter_kind"] else None,
            "schedule_mode": data["schedule_mode"],
            "tolerance_pct": data["tolerance_pct"],
            "estimated_minutes": data["estimated_minutes"],
            "instructions": data["instructions"].strip(),
        }


class PlanItemForm(forms.Form):
    text = forms.CharField(label=_("Tarea"), max_length=250)
    item_type = forms.ChoiceField(label=_("Tipo"), choices=ItemType.choices, initial=ItemType.CHECK)
    unit = forms.CharField(label=_("Unidad"), max_length=20, required=False)
    min_value = forms.DecimalField(label=_("Mínimo"), max_digits=14, decimal_places=3, required=False)
    max_value = forms.DecimalField(label=_("Máximo"), max_digits=14, decimal_places=3, required=False)
    required = forms.BooleanField(label=_("Obligatorio para completar la OT"), required=False, initial=True)

    def clean(self):
        cleaned = super().clean()
        low, high = cleaned.get("min_value"), cleaned.get("max_value")
        if low is not None and high is not None and low > high:
            self.add_error("max_value", _("El máximo no puede ser menor que el mínimo."))
        return cleaned


class PlanResourceForm(forms.Form):
    kind = forms.ChoiceField(label=_("Tipo"), choices=PlanResource.Kind.choices)
    description = forms.CharField(
        label=_("Descripción"),
        max_length=200,
        required=False,
        help_text=_("Con un repuesto del catálogo puedes dejarla vacía: se usa su nombre."),
    )
    quantity = forms.DecimalField(
        label=_("Cantidad"), max_digits=12, decimal_places=3, min_value=0.001, initial=1
    )
    unit = forms.CharField(label=_("Unidad"), max_length=20, required=False)
    # Repuesto del catálogo (INV-11): lo elige el buscador; el servicio valida que sea de la organización.
    part = forms.UUIDField(required=False)

    def clean(self):
        cleaned = super().clean()
        if not cleaned.get("part") and not (cleaned.get("description") or "").strip():
            self.add_error("description", _("Este campo es obligatorio."))
        return cleaned


class PartLinkForm(forms.Form):
    part = forms.UUIDField(error_messages={"required": _("Elige un repuesto del catálogo.")})


class PublishForm(forms.Form):
    change_note = forms.CharField(
        label=_("Qué cambió"),
        max_length=300,
        required=False,
        help_text=_("Queda en el historial de versiones."),
    )


class AssignFilterForm(forms.Form):
    q = forms.CharField(label=_("Buscar activo"), required=False, max_length=100)
    asset_class = _class_field(label=_("Clase de activo"), required=False, empty_label=_("Todas las clases"))
    location = forms.ModelChoiceField(
        label=_("Ubicación"),
        queryset=Location.all_tenants.none(),
        required=False,
        empty_label=_("Todas las ubicaciones"),
    )

    def __init__(self, *args, membership, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["asset_class"].queryset = AssetClass.objects.order_by("name")
        self.fields["location"].queryset = visible_locations(membership=membership).order_by("code")
        self.fields["location"].label_from_instance = _location_label


class AssetPlanForm(forms.Form):
    """Asignar un plan desde la ficha del activo."""

    plan = forms.ModelChoiceField(label=_("Plan"), queryset=None, empty_label=None)
    meter = forms.ModelChoiceField(
        label=_("Medidor"),
        queryset=None,
        required=False,
        empty_label=_("El primero del tipo del plan"),
        help_text=_("Solo para planes por medidor, si el activo tiene varios del mismo tipo."),
    )

    def __init__(self, *args, plans, meters, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["plan"].queryset = plans
        self.fields["meter"].queryset = meters
        self.fields["meter"].label_from_instance = lambda m: m.name or m.get_kind_display()
        if not meters.exists():
            del self.fields["meter"]


class MaintenanceSettingsForm(forms.Form):
    horizon_days = forms.IntegerField(
        label=_("Horizonte de generación (días)"),
        min_value=1,
        max_value=MAX_HORIZON_DAYS,
        help_text=_("Por defecto, 14 días: la OT aparece dos semanas antes de vencer."),
    )


class CalendarFilterForm(forms.Form):
    """Filtros del calendario de preventivos (PM-06): ubicación (con lo que tiene debajo), clase,
    activo y plan."""

    location = forms.ModelChoiceField(
        label=_("Ubicación"),
        queryset=Location.all_tenants.none(),
        required=False,
        empty_label=_("Todas las ubicaciones"),
    )
    asset_class = _class_field(label=_("Clase de activo"), required=False, empty_label=_("Todas las clases"))
    asset = forms.CharField(
        label=_("Activo"), required=False, max_length=100, help_text=_("Código o nombre.")
    )
    plan = forms.ModelChoiceField(
        label=_("Plan"), queryset=MaintenancePlan.all_tenants.none(), required=False, empty_label=_("Todos")
    )

    def __init__(self, *args, membership, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["location"].queryset = visible_locations(membership=membership).order_by("code")
        self.fields["location"].label_from_instance = _location_label
        self.fields["asset_class"].queryset = AssetClass.objects.order_by("name")
        self.fields["plan"].queryset = MaintenancePlan.objects.filter(
            is_active=True, current_version__isnull=False
        ).order_by("name")
