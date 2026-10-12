"""Formularios web de inventario (INV-01, INV-03, INV-12).

Validan la entrada; las reglas de negocio están en los servicios.
"""

import uuid

from django import forms
from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

from apps.organizations.selectors import visible_locations
from apps.work.forms import LocalDateTimeInput

from .models import StockMovement, Warehouse

PART_FIELDS = (
    "code",
    "name",
    "unit",
    "category",
    "manufacturer",
    "manufacturer_ref",
    "supplier",
    "description",
    "notes",
)


def apply_errors(form, error: ValidationError) -> None:
    """Muestra un error de servicio en su campo (o arriba del formulario si el campo no existe)."""
    if hasattr(error, "error_dict"):
        for field, errors in error.message_dict.items():
            form.add_error(field if field in form.fields else None, errors)
    else:
        form.add_error(None, error.messages)


class PartForm(forms.Form):
    code = forms.CharField(
        label=_("Código"), max_length=40, help_text=_("Único en la organización. Ej.: ROD-6205.")
    )
    name = forms.CharField(label=_("Nombre"), max_length=200)
    unit = forms.CharField(label=_("Unidad"), max_length=20, required=False, initial="un")
    category = forms.CharField(
        label=_("Categoría"),
        max_length=80,
        required=False,
        widget=forms.TextInput(attrs={"list": "part-categories", "autocomplete": "off"}),
        help_text=_("Elige una existente o escribe una nueva."),
    )
    manufacturer = forms.CharField(label=_("Fabricante"), max_length=120, required=False)
    manufacturer_ref = forms.CharField(label=_("N° de parte"), max_length=80, required=False)
    supplier = forms.CharField(label=_("Proveedor"), max_length=150, required=False)
    description = forms.CharField(
        label=_("Descripción"), required=False, widget=forms.Textarea(attrs={"rows": 3})
    )
    notes = forms.CharField(label=_("Notas"), required=False, widget=forms.Textarea(attrs={"rows": 2}))

    @classmethod
    def for_part(cls, part, data=None):
        return cls(data, initial={name: getattr(part, name) for name in PART_FIELDS})


class WarehouseForm(forms.Form):
    code = forms.CharField(label=_("Código"), max_length=20, help_text=_("Ej.: BOD, BOD-NORTE."))
    name = forms.CharField(label=_("Nombre"), max_length=120)
    location = forms.ModelChoiceField(
        label=_("Ubicación"),
        queryset=None,
        required=False,
        empty_label=_("Sin ubicación"),
        help_text=_("Opcional: el sitio o área donde está la bodega."),
    )
    notes = forms.CharField(label=_("Notas"), required=False, widget=forms.Textarea(attrs={"rows": 2}))

    def __init__(self, *args, membership, **kwargs):
        super().__init__(*args, **kwargs)
        # Solo ubicaciones activas del alcance de la membresía (INV-03 CA5): un ID de otra
        # organización o fuera del alcance no es una opción válida.
        self.fields["location"].queryset = (
            visible_locations(membership=membership).filter(is_active=True).order_by("name")
        )

    @classmethod
    def for_warehouse(cls, warehouse, *, membership, data=None):
        return cls(
            data,
            membership=membership,
            initial={
                "code": warehouse.code,
                "name": warehouse.name,
                "location": warehouse.location_id,
                "notes": warehouse.notes,
            },
        )


class StockLevelsForm(forms.Form):
    """Mínimo, objetivo y ubicación en bodega de un repuesto en una bodega (INV-12)."""

    min_quantity = forms.DecimalField(
        label=_("Stock mínimo"), min_value=0, max_digits=14, decimal_places=3, initial=0
    )
    target_quantity = forms.DecimalField(
        label=_("Stock objetivo"), min_value=0, max_digits=14, decimal_places=3, required=False
    )
    bin_location = forms.CharField(
        label=_("Ubicación en bodega"), max_length=40, required=False, help_text=_("Ej.: Estante A-3.")
    )

    def __init__(self, *args, prefix=None, **kwargs):
        super().__init__(*args, prefix=prefix, **kwargs)
        for field in self.fields.values():
            if isinstance(field, forms.DecimalField):
                field.widget.attrs.setdefault("inputmode", "decimal")
                field.widget.attrs.setdefault("step", "any")

    def clean(self):
        cleaned = super().clean()
        minimum, target = cleaned.get("min_quantity"), cleaned.get("target_quantity")
        if minimum is not None and target is not None and target < minimum:
            self.add_error("target_quantity", _("El objetivo no puede ser menor que el mínimo."))
        return cleaned


class PartImportForm(forms.Form):
    file = forms.FileField(
        label=_("Archivo Excel (.xlsx) o CSV"),
        widget=forms.ClearableFileInput(
            attrs={
                "accept": ".xlsx,.csv,text/csv,"
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            }
        ),
    )


# --- Movimientos (INV-05, INV-06, INV-07, INV-14) -------------------------------


def _decimal_widget():
    return forms.NumberInput(attrs={"inputmode": "decimal", "step": "any", "min": "0"})


class WarehouseChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, obj):
        return obj.name


def _positive(value):
    if value is not None and value <= 0:
        raise ValidationError(_("La cantidad debe ser mayor que cero."))
    return value


def _setup_warehouse(form, name, *, keep_single=False):
    """Selector entre las bodegas visibles y activas; con una sola se quita (INV-03 CA4)."""
    if len(form.warehouses) > 1 or (keep_single and form.warehouses):
        pks = [warehouse.pk for warehouse in form.warehouses]
        form.fields[name].queryset = Warehouse.objects.filter(pk__in=pks).order_by("-is_default", "name")
    else:
        del form.fields[name]


class _MovementForm(forms.Form):
    """Base de entrada, conteo y transferencia: bodegas visibles y clave de idempotencia oculta."""

    client_uuid = forms.UUIDField(widget=forms.HiddenInput, required=False, initial=uuid.uuid4)

    def __init__(self, *args, warehouses=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.warehouses = list(warehouses)


# El repuesto (`part`) no es campo del formulario: llega como radio del buscador o como campo
# oculto si ya venía elegido, y la vista lo busca entre los de la organización (un ID ajeno no
# se encuentra).


class ReceiveForm(_MovementForm):
    """Entrada de stock (INV-05). La bodega se pide solo con más de una activa (INV-03 CA4)."""

    quantity = forms.DecimalField(
        label=_("Cantidad"), max_digits=14, decimal_places=3, widget=_decimal_widget()
    )
    unit_cost = forms.DecimalField(
        label=_("Costo unitario"),
        max_digits=18,
        decimal_places=4,
        min_value=0,
        required=False,
        widget=_decimal_widget(),
        help_text=_("Opcional. Vacío: entra al costo promedio actual. Con 0, el promedio baja."),
    )
    warehouse = WarehouseChoiceField(label=_("Bodega"), queryset=None, empty_label=None)
    reference = forms.CharField(
        label=_("Documento"), max_length=60, required=False, help_text=_("Factura o guía. Ej.: GD-1234.")
    )
    supplier = forms.CharField(label=_("Proveedor"), max_length=150, required=False)
    note = forms.CharField(
        label=_("Nota"), max_length=500, required=False, widget=forms.Textarea(attrs={"rows": 2})
    )
    occurred_at = forms.DateTimeField(
        label=_("Fecha de la entrada"),
        required=False,
        widget=LocalDateTimeInput(),
        help_text=_("Vacía: ahora. Hasta 30 días atrás."),
    )

    def __init__(self, *args, warehouses=(), show_costs=True, **kwargs):
        super().__init__(*args, warehouses=warehouses, **kwargs)
        _setup_warehouse(self, "warehouse")
        if not show_costs:
            del self.fields["unit_cost"]

    def clean_quantity(self):
        return _positive(self.cleaned_data["quantity"])


class CountForm(_MovementForm):
    """Conteo de un repuesto en una bodega (INV-06). El motivo lo exige el servicio si corresponde."""

    warehouse = WarehouseChoiceField(label=_("Bodega"), queryset=None, empty_label=None)
    counted_quantity = forms.DecimalField(
        label=_("Cantidad contada"), max_digits=14, decimal_places=3, min_value=0, widget=_decimal_widget()
    )
    note = forms.CharField(
        label=_("Motivo"),
        max_length=500,
        required=False,
        widget=forms.Textarea(attrs={"rows": 2}),
        help_text=_("Obligatorio si faltan unidades o si la diferencia supera el 10 %."),
    )

    def __init__(self, *args, warehouses=(), **kwargs):
        super().__init__(*args, warehouses=warehouses, **kwargs)
        _setup_warehouse(self, "warehouse")


class TransferForm(_MovementForm):
    """Transferencia entre bodegas (INV-07): solo existe con más de una bodega activa."""

    from_warehouse = WarehouseChoiceField(label=_("Desde"), queryset=None, empty_label=None)
    to_warehouse = WarehouseChoiceField(label=_("Hacia"), queryset=None, empty_label=None)
    quantity = forms.DecimalField(
        label=_("Cantidad"), max_digits=14, decimal_places=3, widget=_decimal_widget()
    )
    note = forms.CharField(
        label=_("Nota"), max_length=500, required=False, widget=forms.Textarea(attrs={"rows": 2})
    )

    def __init__(self, *args, warehouses=(), **kwargs):
        super().__init__(*args, warehouses=warehouses, **kwargs)
        for name in ("from_warehouse", "to_warehouse"):
            _setup_warehouse(self, name, keep_single=True)
        if len(self.warehouses) > 1 and "from_warehouse" not in self.initial:
            self.initial.setdefault("from_warehouse", self.warehouses[0].pk)
            self.initial.setdefault("to_warehouse", self.warehouses[1].pk)

    def clean_quantity(self):
        return _positive(self.cleaned_data["quantity"])

    def clean(self):
        cleaned = super().clean()
        source, target = cleaned.get("from_warehouse"), cleaned.get("to_warehouse")
        if source is not None and source == target:
            self.add_error("to_warehouse", _("Elige una bodega de destino distinta a la de origen."))
        return cleaned


class MovementFilterForm(forms.Form):
    """Filtros del libro de movimientos (INV-14 CA1). Los inválidos se ignoran."""

    part = forms.CharField(
        label=_("Repuesto"),
        max_length=200,
        required=False,
        widget=forms.TextInput(attrs={"placeholder": _("Código o nombre")}),
    )
    warehouse = WarehouseChoiceField(
        label=_("Bodega"), queryset=None, required=False, empty_label=_("Todas las bodegas")
    )
    kind = forms.ChoiceField(label=_("Tipo"), required=False)
    date_from = forms.DateField(
        label=_("Desde"), required=False, widget=forms.DateInput(attrs={"type": "date"})
    )
    date_to = forms.DateField(
        label=_("Hasta"), required=False, widget=forms.DateInput(attrs={"type": "date"})
    )
    work_order = forms.CharField(
        label=_("OT"),
        max_length=20,
        required=False,
        widget=forms.TextInput(attrs={"placeholder": _("Ej.: OT-C-00012")}),
    )

    def __init__(self, *args, warehouses=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.warehouses = list(warehouses)
        _setup_warehouse(self, "warehouse")
        self.fields["kind"].choices = [("", _("Todos los tipos")), *StockMovement.Kind.choices]

    def clean(self):
        cleaned = super().clean()
        start, end = cleaned.get("date_from"), cleaned.get("date_to")
        if start and end and end < start:
            self.add_error("date_to", _("La fecha final no puede ser anterior a la inicial."))
        return cleaned

    @property
    def active_count(self) -> int:
        return sum(1 for value in getattr(self, "cleaned_data", {}).values() if value)

    def filters(self) -> dict:
        """Argumentos de `movement_list`: solo los filtros válidos."""
        if self.is_bound:
            self.is_valid()
        data = getattr(self, "cleaned_data", {})
        return {
            "part": data.get("part", ""),
            "warehouse": data.get("warehouse"),
            "kind": data.get("kind", ""),
            "date_from": data.get("date_from"),
            "date_to": data.get("date_to"),
            "work_order": data.get("work_order", ""),
        }
