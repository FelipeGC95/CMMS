from django import forms
from django.utils.translation import gettext_lazy as _

from apps.assets.models import AssetClass, MeterKind
from apps.maintenance.templates_library import available_templates
from apps.organizations.models import Location

from .services import MAX_AREAS


class IndustryForm(forms.Form):
    industry = forms.ChoiceField(label=_("Rubro"), widget=forms.RadioSelect)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.templates = available_templates()
        self.fields["industry"].choices = [(t["industry"], t["name"]) for t in self.templates]

    def options(self):
        """Cada opción con su rubro (nombre, descripción, totales) para mostrarlos juntos."""
        return list(zip(self["industry"], self.templates, strict=True))


class LocationsForm(forms.Form):
    site_name = forms.CharField(
        label=_("Nombre del sitio"), max_length=150, help_text=_("Tu planta, faena o edificio principal.")
    )
    areas = forms.CharField(
        label=_("Áreas (una por línea, opcional)"),
        required=False,
        widget=forms.Textarea(attrs={"rows": 4}),
        help_text=_("Ej.: Producción, Bodega, Taller. Hasta %(n)s.") % {"n": MAX_AREAS},
    )

    def clean_areas(self):
        return [line.strip() for line in self.cleaned_data["areas"].splitlines() if line.strip()]


class AssetRowForm(forms.Form):
    name = forms.CharField(label=_("Nombre"), max_length=150, required=False)
    asset_class = forms.ModelChoiceField(
        label=_("Clase"), queryset=AssetClass.all_tenants.none(), required=False
    )
    location = forms.ModelChoiceField(
        label=_("Ubicación"), queryset=Location.all_tenants.none(), required=False
    )
    meter_kind = forms.ChoiceField(
        label=_("Medidor"), choices=[("", _("Sin medidor")), *MeterKind.choices], required=False
    )

    def __init__(self, *args, classes=None, locations=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["asset_class"].queryset = classes
        self.fields["location"].queryset = locations

    def clean(self):
        data = super().clean()
        if data.get("name") and not data.get("asset_class"):
            self.add_error("asset_class", _("Elige la clase del activo."))
        return data


AssetRowFormSet = forms.formset_factory(AssetRowForm, extra=5, max_num=20)


class PlansForm(forms.Form):
    plan_codes = forms.MultipleChoiceField(
        label=_("Planes a crear"), widget=forms.CheckboxSelectMultiple, required=False
    )

    def __init__(self, *args, plans=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.plans = list(plans)
        self.fields["plan_codes"].choices = [(p["code"], p["name"]) for p in self.plans]
        self.fields["plan_codes"].initial = [p["code"] for p in self.plans]

    def options(self):
        return list(zip(self["plan_codes"], self.plans, strict=True))


class ReadingsForm(forms.Form):
    """Un campo por medidor; en blanco = sin lectura por ahora."""

    def __init__(self, *args, meters=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.meters = list(meters)
        for meter in self.meters:
            self.fields[f"meter_{meter.pk}"] = forms.DecimalField(
                label=f"{meter.asset.code} · {meter.get_kind_display()}",
                required=False,
                min_value=0,
                max_digits=14,
                decimal_places=3,
                initial=meter.current_value,
            )

    def rows(self):
        return [(meter, self[f"meter_{meter.pk}"]) for meter in self.meters]

    def values(self):
        return {meter.pk: self.cleaned_data.get(f"meter_{meter.pk}") for meter in self.meters}
