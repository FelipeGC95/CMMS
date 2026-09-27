from django import forms
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.assets.models import Asset, AssetClass
from apps.assets.selectors import visible_assets
from apps.organizations.models import Location
from apps.organizations.selectors import visible_locations

from .selectors import KpiFilters

PERIODS = {"30": 30, "90": 90, "365": 365}
CUSTOM = "custom"


class DashboardFilterForm(forms.Form):
    """Filtros del tablero (KPI-01 CA1); todos opcionales y dentro del alcance de la membresía."""

    period = forms.ChoiceField(
        label=_("Período"),
        required=False,
        initial="30",
        choices=[
            ("30", _("Últimos 30 días")),
            ("90", _("Últimos 90 días")),
            ("365", _("Último año")),
            (CUSTOM, _("Personalizado")),
        ],
    )
    date_from = forms.DateField(
        label=_("Desde"), required=False, widget=forms.DateInput(attrs={"type": "date"})
    )
    date_to = forms.DateField(
        label=_("Hasta"), required=False, widget=forms.DateInput(attrs={"type": "date"})
    )
    location = forms.ModelChoiceField(
        # `all_tenants`: al definir la clase no hay organización activa; se reemplaza en __init__.
        label=_("Sitio o ubicación"),
        queryset=Location.all_tenants.none(),
        required=False,
        empty_label=_("Todas"),
        help_text=_("Incluye las ubicaciones que contiene."),
    )
    asset_class = forms.ModelChoiceField(
        label=_("Clase de activo"),
        queryset=AssetClass.all_tenants.none(),
        required=False,
        empty_label=_("Todas"),
    )
    criticality = forms.ChoiceField(
        label=_("Criticidad"), required=False, choices=[("", _("Todas")), *Asset.Criticality.choices]
    )
    asset = forms.CharField(
        label=_("Activo"), required=False, max_length=30, help_text=_("Código exacto de un activo.")
    )

    def __init__(self, *args, membership, **kwargs):
        super().__init__(*args, **kwargs)
        self.membership = membership
        self.fields["location"].queryset = (
            visible_locations(membership=membership).filter(is_active=True).order_by("code")
        )
        self.fields["location"].label_from_instance = lambda loc: f"{loc.name} ({loc.code})"
        self.fields["asset_class"].queryset = AssetClass.objects.filter(is_active=True).order_by("name")

    def clean_asset(self):
        code = self.cleaned_data["asset"].strip()
        if not code:
            return None
        asset = visible_assets(membership=self.membership).filter(code__iexact=code).first()
        if asset is None:
            raise forms.ValidationError(_("No hay un activo visible con ese código."))
        return asset

    def clean(self):
        data = super().clean()
        if data.get("period") == CUSTOM:
            date_from, date_to = data.get("date_from"), data.get("date_to")
            if not date_from:
                self.add_error("date_from", _("Indica desde qué día medir."))
            elif date_from > timezone.localdate():
                self.add_error("date_from", _("La fecha de inicio no puede ser futura."))
            if date_from and date_to and date_to < date_from:
                self.add_error("date_to", _("La fecha de término debe ser igual o posterior a la de inicio."))
        return data

    def filters(self) -> KpiFilters:
        """Filtros válidos; un campo con error se ignora (el tablero siempre se muestra)."""
        data = {key: value for key, value in self.cleaned_data.items() if key not in self.errors}
        custom = (
            data.get("period") == CUSTOM and "date_from" not in self.errors and "date_to" not in self.errors
        )
        return KpiFilters(
            days=PERIODS.get(data.get("period") or "30", 30),
            date_from=data.get("date_from") if custom else None,
            date_to=(data.get("date_to") or None) if custom else None,
            location=data.get("location"),
            asset_class=data.get("asset_class"),
            criticality=data.get("criticality") or "",
            asset=data.get("asset"),
        )

    @property
    def active_count(self) -> int:
        data = getattr(self, "cleaned_data", {})
        count = sum(1 for key in ("location", "asset_class", "criticality", "asset") if data.get(key))
        return count + (1 if data.get("period") not in ("", "30", None) else 0)
