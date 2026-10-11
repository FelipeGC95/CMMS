"""Formularios web de inventario (INV-01, INV-03, INV-12).

Validan la entrada; las reglas de negocio están en los servicios.
"""

from django import forms
from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

from apps.organizations.selectors import visible_locations

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
