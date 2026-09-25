import uuid

from django import forms
from django.utils.translation import gettext_lazy as _

from apps.accounts.models import User
from apps.assets.models import Asset
from apps.assets.selectors import visible_assets
from apps.organizations.models import Location
from apps.organizations.selectors import visible_locations

from .models import FailureCode, WorkOrder
from .selectors import assignable_members, failure_codes


class FailureReportForm(forms.Form):
    description = forms.CharField(
        label=_("¿Qué pasó?"),
        widget=forms.Textarea(attrs={"rows": 4, "placeholder": _("Ej: pierde aceite por el costado")}),
        max_length=2000,
    )
    asset_stopped = forms.BooleanField(label=_("El equipo está detenido"), required=False)
    photo = forms.FileField(
        label=_("Foto (opcional)"),
        required=False,
        widget=forms.ClearableFileInput(attrs={"accept": "image/*", "capture": "environment"}),
    )
    # Generado al mostrar el formulario: un doble envío no crea dos avisos.
    client_uuid = forms.UUIDField(widget=forms.HiddenInput, required=False, initial=uuid.uuid4)


class CompleteWorkOrderForm(forms.Form):
    labor_minutes = forms.IntegerField(label=_("Tiempo trabajado (minutos)"), min_value=0, max_value=24 * 60)
    notes = forms.CharField(
        label=_("Comentario de cierre"), required=False, widget=forms.Textarea(attrs={"rows": 3})
    )
    failure_mode = forms.ModelChoiceField(
        label=_("Modo de falla"), queryset=FailureCode.all_tenants.none(), required=False
    )
    failure_cause = forms.ModelChoiceField(
        label=_("Causa"), queryset=FailureCode.all_tenants.none(), required=False
    )

    meter_value = forms.DecimalField(
        label=_("Lectura del medidor"), min_value=0, max_digits=14, decimal_places=3
    )

    def __init__(self, *args, with_failure=False, meter=None, meter_required=False, **kwargs):
        super().__init__(*args, **kwargs)
        if meter is None:
            del self.fields["meter_value"]
        else:
            field = self.fields["meter_value"]
            field.label = f"{meter.get_kind_display()} al terminar"
            field.required = meter_required
            field.help_text = (
                _("Obligatorio: el próximo preventivo se calcula desde esta lectura.")
                if meter_required
                else _("Opcional: actualiza la lectura del equipo.")
            )
            if meter.current_value is not None:
                field.widget.attrs["placeholder"] = f"{meter.current_value.normalize():f}"
        if with_failure:
            self.fields["failure_mode"].queryset = failure_codes(FailureCode.Kind.MODE)
            self.fields["failure_cause"].queryset = failure_codes(FailureCode.Kind.CAUSE)
        else:
            del self.fields["failure_mode"]
            del self.fields["failure_cause"]


class LaborForm(forms.Form):
    minutes = forms.IntegerField(label=_("Minutos trabajados"), min_value=1, max_value=24 * 60)


class PartUsageForm(forms.Form):
    description = forms.CharField(label=_("Repuesto o material"), max_length=200)
    quantity = forms.DecimalField(label=_("Cantidad"), min_value=0, max_digits=12, decimal_places=3)
    unit = forms.CharField(label=_("Unidad"), max_length=20, required=False)
    unit_cost = forms.DecimalField(label=_("Costo unitario"), min_value=0, max_digits=14, decimal_places=2)
    # Generado al mostrar el formulario: un doble envío no duplica la línea.
    client_uuid = forms.UUIDField(widget=forms.HiddenInput, required=False, initial=uuid.uuid4)


class OtherCostForm(forms.Form):
    description = forms.CharField(label=_("Descripción"), max_length=200)
    supplier = forms.CharField(label=_("Proveedor"), max_length=150, required=False)
    amount = forms.DecimalField(label=_("Monto"), min_value=0, max_digits=14, decimal_places=2)


class WorkOrderFilterForm(forms.Form):
    """Filtros de la lista de OT (WO-05); todos opcionales, se combinan con la vista (scope)."""

    DATE_FIELD_CHOICES = [("due_on", _("Fecha límite")), ("created_at", _("Fecha de creación"))]

    status = forms.ChoiceField(
        label=_("Estado"), choices=[("", _("Todos los estados")), *WorkOrder.Status.choices], required=False
    )
    asset_q = forms.CharField(
        label=_("Activo"), required=False, max_length=200, help_text=_("Código o nombre.")
    )
    location = forms.ModelChoiceField(
        # `all_tenants`: al definir la clase no hay organización activa en el contexto.
        label=_("Sitio"),
        queryset=Location.all_tenants.none(),
        required=False,
        empty_label=_("Todos los sitios"),
    )
    assigned_to = forms.ModelChoiceField(
        label=_("Responsable"), queryset=User.objects.none(), required=False, empty_label=_("Todos")
    )
    date_field = forms.ChoiceField(
        label=_("Buscar fecha en"), choices=DATE_FIELD_CHOICES, required=False, initial="due_on"
    )
    date_from = forms.DateField(
        label=_("Desde"), required=False, widget=forms.DateInput(attrs={"type": "date"})
    )
    date_to = forms.DateField(
        label=_("Hasta"), required=False, widget=forms.DateInput(attrs={"type": "date"})
    )

    def __init__(self, *args, membership, **kwargs):
        super().__init__(*args, **kwargs)
        # code/name en vez de str(location): evita recorrer `parent` por cada opción (N+1).
        self.fields["location"].queryset = visible_locations(membership=membership).order_by("code")
        self.fields["location"].label_from_instance = lambda loc: f"{loc.name} ({loc.code})"
        self.fields["assigned_to"].queryset = assignable_members(membership=membership)

    def clean(self):
        cleaned = super().clean()
        date_from, date_to = cleaned.get("date_from"), cleaned.get("date_to")
        if date_from and date_to and date_from > date_to:
            raise forms.ValidationError(_("La fecha «desde» no puede ser posterior a «hasta»."))
        return cleaned


class WorkOrderCreateForm(forms.Form):
    """Alta manual de OT (WO-01); el correlativo y el estado inicial los pone el servicio."""

    work_type = forms.ChoiceField(label=_("Tipo"), choices=WorkOrder.Type.choices)
    asset = forms.ModelChoiceField(label=_("Activo"), queryset=Asset.all_tenants.none())
    title = forms.CharField(label=_("Título"), max_length=200)
    description = forms.CharField(
        label=_("Descripción"), required=False, widget=forms.Textarea(attrs={"rows": 4})
    )
    priority = forms.TypedChoiceField(
        label=_("Prioridad"), choices=WorkOrder.Priority.choices, coerce=int, initial=WorkOrder.Priority.NORMAL
    )
    due_on = forms.DateField(
        label=_("Fecha límite"), required=False, widget=forms.DateInput(attrs={"type": "date"})
    )
    estimated_minutes = forms.IntegerField(
        label=_("Estimación (minutos)"), required=False, min_value=1, max_value=30 * 24 * 60
    )
    assigned_to = forms.ModelChoiceField(
        label=_("Responsable"), queryset=User.objects.none(), required=False, empty_label=_("Sin asignar")
    )

    def __init__(self, *args, membership, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["asset"].queryset = (
            visible_assets(membership=membership).filter(is_active=True).order_by("code")
        )
        self.fields["assigned_to"].queryset = assignable_members(membership=membership)
