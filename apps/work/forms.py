import base64
import binascii
import uuid
from decimal import Decimal

from django import forms
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils.translation import gettext_lazy as _

from apps.accounts.antibot import HumanCheckMixin
from apps.accounts.models import User
from apps.assets.models import Asset
from apps.assets.selectors import visible_assets
from apps.maintenance.models import ItemType
from apps.organizations.models import Location
from apps.organizations.selectors import visible_locations
from apps.ui.templatetags.ui import work_order_status_label

from .models import FailureCode, FailureEvent, WorkOrder
from .selectors import OPEN_STATUSES, assignable_members, failure_codes


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


class AnonymousReportForm(HumanCheckMixin, forms.Form):
    """Aviso por QR sin cuenta (REQ-02 CA1), con anti-bots (CA2): ver `apps.accounts.antibot`."""

    challenge_salt = "work.anonymous_report.challenge"
    min_seconds_setting = "QR_REPORT_MIN_SECONDS"

    name = forms.CharField(label=_("Tu nombre"), max_length=100)
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

    def clean_photo(self):
        photo = self.cleaned_data.get("photo")
        if photo and not (photo.content_type or "").startswith("image/"):
            raise forms.ValidationError(_("La foto debe ser una imagen."))
        return photo


def _location_label(location):
    # code/name en vez de str(location): evita recorrer `parent` por cada opción (N+1).
    return f"{location.name} ({location.code})"


def _active_locations(membership):
    return visible_locations(membership=membership).filter(is_active=True).order_by("code")


class LocationRequestForm(forms.Form):
    """Aviso sobre una ubicación (REQ-01 CA1): sin "equipo detenido", que solo aplica a un activo."""

    location = forms.ModelChoiceField(
        label=_("Ubicación"),
        queryset=Location.all_tenants.none(),
        empty_label=_("Elige dónde es el problema"),
    )
    description = forms.CharField(
        label=_("¿Qué pasó o qué se necesita?"),
        widget=forms.Textarea(attrs={"rows": 4, "placeholder": _("Ej: gotera sobre la línea 2")}),
        max_length=2000,
    )
    photo = forms.FileField(
        label=_("Foto (opcional)"),
        required=False,
        widget=forms.ClearableFileInput(attrs={"accept": "image/*", "capture": "environment"}),
    )
    client_uuid = forms.UUIDField(widget=forms.HiddenInput, required=False, initial=uuid.uuid4)

    def __init__(self, *args, membership, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["location"].queryset = _active_locations(membership)
        self.fields["location"].label_from_instance = _location_label


class LocalDateTimeInput(forms.DateTimeInput):
    """`datetime-local` nativo: el teléfono muestra su selector de fecha y hora."""

    input_type = "datetime-local"

    def __init__(self, attrs=None):
        super().__init__(attrs=attrs, format="%Y-%m-%dT%H:%M")


# Campos aptos para dictado por voz (ADR-018): texto libre, sin límite corto.
DICTATION_ATTRS = {"rows": 3, "autocapitalize": "sentences", "spellcheck": "true"}


SIGNATURE_PREFIX = "data:image/png;base64,"
SIGNATURE_MAX_CHARS = 1_500_000  # ~1 MB de PNG: una firma real pesa decenas de KB.


def decode_signature(data: str):
    """Firma dibujada (data URL PNG de static/js/signature-pad.js) → archivo; vacío = None."""
    if not data:
        return None
    if not data.startswith(SIGNATURE_PREFIX):
        raise forms.ValidationError(_("La firma no es válida. Bórrala y vuelve a firmar."))
    try:
        content = base64.b64decode(data[len(SIGNATURE_PREFIX) :], validate=True)
    except (binascii.Error, ValueError) as error:
        raise forms.ValidationError(_("La firma no es válida. Bórrala y vuelve a firmar.")) from error
    return SimpleUploadedFile("firma.png", content, content_type="image/png")


class ChecklistSignatureForm(forms.Form):
    """Firma de un ítem «firma» del checklist (PM-01 CA1); se dibuja igual que la de cierre."""

    signature = forms.CharField(
        label=_("Firma"), widget=forms.HiddenInput, required=True, max_length=SIGNATURE_MAX_CHARS
    )

    def clean_signature(self):
        return decode_signature(self.cleaned_data.get("signature", ""))


class CompleteWorkOrderForm(forms.Form):
    """Cierre de OT. En correctivas pide síntoma, acción y la falla (REQ-03 CA1–CA3)."""

    symptom = forms.CharField(
        label=_("Síntoma observado"),
        help_text=_("¿Qué viste? Puedes dictarlo con el micrófono del teclado."),
        required=False,
        max_length=4000,
        widget=forms.Textarea(attrs=DICTATION_ATTRS),
    )
    notes = forms.CharField(
        label=_("Comentario de cierre"),
        required=False,
        max_length=4000,
        widget=forms.Textarea(attrs=DICTATION_ATTRS),
    )
    labor_minutes = forms.IntegerField(label=_("Tiempo trabajado (minutos)"), min_value=0, max_value=24 * 60)
    impact = forms.ChoiceField(label=_("Impacto"), choices=FailureEvent.Impact.choices)
    failure_started_at = forms.DateTimeField(
        label=_("Inicio de la detención"),
        help_text=_("Si no hubo detención: cuándo se detectó la falla."),
        required=False,
        widget=LocalDateTimeInput(),
    )
    failure_ended_at = forms.DateTimeField(
        label=_("Fin de la detención"),
        help_text=_("Si lo dejas vacío, se usa la hora actual."),
        required=False,
        widget=LocalDateTimeInput(),
    )
    failure_mode = forms.ModelChoiceField(
        label=_("Modo de falla"), queryset=FailureCode.all_tenants.none(), required=False
    )
    failure_cause = forms.ModelChoiceField(
        label=_("Causa"), queryset=FailureCode.all_tenants.none(), required=False
    )
    failure_mechanism = forms.ModelChoiceField(
        label=_("Mecanismo de falla"), queryset=FailureCode.all_tenants.none(), required=False
    )
    detection_method = forms.ModelChoiceField(
        label=_("Método de detección"), queryset=FailureCode.all_tenants.none(), required=False
    )
    component = forms.ModelChoiceField(
        label=_("Componente afectado"), queryset=Asset.all_tenants.none(), required=False
    )

    meter_value = forms.DecimalField(
        label=_("Lectura del medidor"), min_value=0, max_digits=14, decimal_places=3
    )
    # Firma dibujada en pantalla (WO-03 CA3): static/js/signature-pad.js deja aquí un PNG en base64.
    signature = forms.CharField(widget=forms.HiddenInput, required=False, max_length=SIGNATURE_MAX_CHARS)

    FAILURE_FIELDS = (
        "symptom",
        "impact",
        "failure_started_at",
        "failure_ended_at",
        "failure_mode",
        "failure_cause",
        "failure_mechanism",
        "detection_method",
        "component",
    )

    def __init__(
        self, *args, with_failure=False, meter=None, meter_required=False, require_failure_details=False,
        asset=None, **kwargs,
    ):  # fmt: skip
        super().__init__(*args, **kwargs)
        if meter is None:
            del self.fields["meter_value"]
        else:
            field = self.fields["meter_value"]
            field.label = _("%(kind)s al terminar") % {"kind": meter.get_kind_display()}
            field.required = meter_required
            field.help_text = (
                _("Obligatorio: el próximo preventivo se calcula desde esta lectura.")
                if meter_required
                else _("Opcional: actualiza la lectura del equipo.")
            )
            if meter.current_value is not None:
                field.widget.attrs["placeholder"] = f"{meter.current_value.normalize():f}"
        if with_failure:
            notes = self.fields["notes"]
            notes.label = _("Acción realizada")
            notes.help_text = _("¿Qué hiciste? Puedes dictarlo con el micrófono del teclado.")
            self.fields["failure_mode"].queryset = failure_codes(FailureCode.Kind.MODE)
            self.fields["failure_cause"].queryset = failure_codes(FailureCode.Kind.CAUSE)
            self.fields["failure_mechanism"].queryset = failure_codes(FailureCode.Kind.MECHANISM)
            self.fields["detection_method"].queryset = failure_codes(FailureCode.Kind.DETECTION_METHOD)
            components = asset.children.filter(is_active=True) if asset is not None else None
            if components is not None and components.exists():
                self.fields["component"].queryset = components
            else:
                del self.fields["component"]  # sin hijos no hay componente que elegir
            if require_failure_details:
                self.fields["failure_mode"].required = True
                self.fields["failure_cause"].required = True
        else:
            for name in self.FAILURE_FIELDS:
                del self.fields[name]
            self.order_fields(["labor_minutes", "notes"])

    def clean_signature(self):
        """Convierte la firma (data URL PNG) en un archivo; vacío = sin firma (es opcional)."""
        return decode_signature(self.cleaned_data.get("signature", ""))

    def clean(self):
        cleaned = super().clean()
        started, ended = cleaned.get("failure_started_at"), cleaned.get("failure_ended_at")
        if started and ended and ended < started:
            self.add_error("failure_ended_at", _("El fin no puede ser anterior al inicio."))
        return cleaned


class FailureCodeForm(forms.Form):
    kind = forms.ChoiceField(label=_("Tipo"), choices=FailureCode.Kind.choices)
    code = forms.CharField(label=_("Código"), max_length=10)
    name = forms.CharField(label=_("Nombre"), max_length=120)


class FailureCodeRenameForm(forms.Form):
    name = forms.CharField(label=_("Nombre"), max_length=120)


class LaborForm(forms.Form):
    minutes = forms.IntegerField(label=_("Minutos trabajados"), min_value=1, max_value=24 * 60)


class PartUsageForm(forms.Form):
    description = forms.CharField(label=_("Repuesto o material"), max_length=200)
    quantity = forms.DecimalField(label=_("Cantidad"), min_value=0, max_digits=12, decimal_places=3)
    unit = forms.CharField(label=_("Unidad"), max_length=20, required=False)
    unit_cost = forms.DecimalField(label=_("Costo unitario"), min_value=0, max_digits=14, decimal_places=2)
    # Generado al mostrar el formulario: un doble envío no duplica la línea.
    client_uuid = forms.UUIDField(widget=forms.HiddenInput, required=False, initial=uuid.uuid4)


class CatalogPartUsageForm(forms.Form):
    """Repuesto del catálogo usado en la OT (INV-09): el costo lo fija el servidor (promedio vigente)."""

    part = forms.UUIDField()
    quantity = forms.DecimalField(
        label=_("Cantidad"), min_value=Decimal("0.001"), max_digits=12, decimal_places=3
    )
    # Vacía: la bodega sale sola (reserva → la más cercana al activo → por defecto).
    warehouse = forms.UUIDField(required=False)
    client_uuid = forms.UUIDField(required=False)


class PlannedPartsForm(forms.Form):
    """Repuestos previstos pendientes al completar en la web (INV, decisión Q2).

    Cada reserva activa con pendiente aparece pre-marcada con su pendiente como
    cantidad; se desmarca o se edita con un toque. `parts_used()` entrega lo marcado
    para `complete_work_order(parts_used=...)`.
    """

    prefix = "planned"

    def __init__(self, data=None, *, planned=(), **kwargs):
        super().__init__(data, **kwargs)
        self.planned = [p for p in planned if p.reservation.status == "active" and p.pending > 0]
        for planned_part in self.planned:
            key = planned_part.reservation.pk.hex
            self.fields[f"use_{key}"] = forms.BooleanField(
                label=str(planned_part.part), required=False, initial=True
            )
            self.fields[f"qty_{key}"] = forms.DecimalField(
                label=_("Cantidad"),
                required=False,
                min_value=Decimal("0.001"),
                max_digits=12,
                decimal_places=3,
                initial=planned_part.pending_input,
                widget=forms.NumberInput(
                    attrs={"class": "input part-row__qty num", "step": "any", "inputmode": "decimal"}
                ),
            )

    def rows(self):
        for planned_part in self.planned:
            key = planned_part.reservation.pk.hex
            yield planned_part, self[f"use_{key}"], self[f"qty_{key}"]

    def clean(self):
        cleaned = super().clean()
        for planned_part in self.planned:
            key = planned_part.reservation.pk.hex
            if cleaned.get(f"use_{key}") and cleaned.get(f"qty_{key}") is None:
                self.add_error(f"qty_{key}", _("Indica la cantidad usada o desmarca el repuesto."))
        return cleaned

    def parts_used(self) -> list[dict]:
        return [
            {
                "part": planned_part.part,
                "quantity": self.cleaned_data[f"qty_{planned_part.reservation.pk.hex}"],
            }
            for planned_part in self.planned
            if self.cleaned_data.get(f"use_{planned_part.reservation.pk.hex}")
        ]


class ReservationForm(forms.Form):
    """Reserva manual de un repuesto para la OT (INV-10, `work_order.manage`)."""

    part = forms.UUIDField()
    quantity = forms.DecimalField(
        label=_("Cantidad"), min_value=Decimal("0.001"), max_digits=12, decimal_places=3
    )
    warehouse = forms.UUIDField(required=False)


class OtherCostForm(forms.Form):
    description = forms.CharField(label=_("Descripción"), max_length=200)
    supplier = forms.CharField(label=_("Proveedor"), max_length=150, required=False)
    amount = forms.DecimalField(label=_("Monto"), min_value=0, max_digits=14, decimal_places=2)


class WorkOrderFilterForm(forms.Form):
    """Filtros de la lista de OT (WO-05); todos opcionales, se combinan con la vista (scope)."""

    DATE_FIELD_CHOICES = [("due_on", _("Fecha límite")), ("created_at", _("Fecha de creación"))]

    status = forms.ChoiceField(label=_("Estado"), choices=(), required=False)
    asset_q = forms.CharField(
        label=_("Activo o ubicación"), required=False, max_length=200, help_text=_("Código o nombre.")
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
        # Nombres de estado propios de la organización (WO-02 CA5).
        self.fields["status"].choices = [
            ("", _("Todos los estados")),
            *((value, work_order_status_label(value)) for value in WorkOrder.Status.values),
        ]
        self.fields["location"].queryset = visible_locations(membership=membership).order_by("code")
        self.fields["location"].label_from_instance = _location_label
        self.fields["assigned_to"].queryset = assignable_members(membership=membership)

    def clean(self):
        cleaned = super().clean()
        date_from, date_to = cleaned.get("date_from"), cleaned.get("date_to")
        if date_from and date_to and date_from > date_to:
            raise forms.ValidationError(_("La fecha «desde» no puede ser posterior a «hasta»."))
        return cleaned


class BacklogFilterForm(forms.Form):
    """Filtros del backlog y del tablero semanal (REQ-04/05); todos opcionales."""

    location = forms.ModelChoiceField(
        # `all_tenants`: al definir la clase no hay organización activa en el contexto.
        label=_("Sitio"),
        queryset=Location.all_tenants.none(),
        required=False,
        empty_label=_("Todos los sitios"),
    )
    work_type = forms.ChoiceField(label=_("Tipo"), choices=(), required=False)
    status = forms.ChoiceField(label=_("Estado"), choices=(), required=False)
    assigned_to = forms.ModelChoiceField(
        label=_("Responsable"), queryset=User.objects.none(), required=False, empty_label=_("Todos")
    )

    def __init__(self, *args, membership, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["location"].queryset = visible_locations(membership=membership).order_by("code")
        self.fields["location"].label_from_instance = _location_label
        self.fields["location"].widget.attrs["class"] = "input"  # también va suelto en el tablero
        self.fields["work_type"].choices = [("", _("Todos los tipos")), *WorkOrder.Type.choices]
        self.fields["status"].choices = [
            ("", _("Todos los estados")),
            *((value, work_order_status_label(value)) for value in OPEN_STATUSES),
        ]
        self.fields["assigned_to"].queryset = assignable_members(membership=membership)

    @property
    def active_count(self) -> int:
        return sum(1 for value in getattr(self, "cleaned_data", {}).values() if value)


class ScheduleForm(forms.Form):
    """Programar una OT desde el tablero sin arrastrar (REQ-05): día y, si se quiere, responsable."""

    scheduled_for = forms.DateField(label=_("Día"), widget=forms.DateInput(attrs={"type": "date"}))
    assigned_to = forms.ModelChoiceField(
        label=_("Responsable"),
        queryset=User.objects.none(),
        required=False,
        empty_label=_("Sin cambio"),
    )

    def __init__(self, *args, membership, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["assigned_to"].queryset = assignable_members(membership=membership)


class HelpersField(forms.ModelMultipleChoiceField):
    """Responsables de apoyo: casillas (más fáciles de tocar en el teléfono que una lista múltiple)."""

    widget = forms.CheckboxSelectMultiple

    def __init__(self, **kwargs):
        super().__init__(
            label=_("Apoyo"),
            queryset=User.objects.none(),
            required=False,
            help_text=_("Otras personas que trabajan en la OT; también la ven en «Mis OT»."),
            **kwargs,
        )


class WorkOrderCreateForm(forms.Form):
    """Alta manual de OT (WO-01); el correlativo y el estado inicial los pone el servicio.

    El objetivo es un activo o una ubicación (CA2): se elige uno de los dos.
    """

    work_type = forms.ChoiceField(label=_("Tipo"), choices=WorkOrder.Type.choices)
    asset = forms.ModelChoiceField(
        label=_("Activo"),
        queryset=Asset.all_tenants.none(),
        required=False,
        empty_label=_("Elige un activo"),
    )
    location = forms.ModelChoiceField(
        label=_("Ubicación"),
        queryset=Location.all_tenants.none(),
        required=False,
        empty_label=_("Ninguna (la OT es sobre un activo)"),
        help_text=_("Solo si el trabajo no es sobre un activo: un área, una línea o un edificio."),
    )
    title = forms.CharField(label=_("Título"), max_length=200)
    description = forms.CharField(
        label=_("Descripción"), required=False, widget=forms.Textarea(attrs={"rows": 4})
    )
    priority = forms.TypedChoiceField(
        label=_("Prioridad"),
        choices=WorkOrder.Priority.choices,
        coerce=int,
        initial=WorkOrder.Priority.NORMAL,
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
    helpers = HelpersField()

    def __init__(self, *args, membership, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["asset"].queryset = (
            visible_assets(membership=membership).filter(is_active=True).order_by("code")
        )
        self.fields["location"].queryset = _active_locations(membership)
        self.fields["location"].label_from_instance = _location_label
        members = assignable_members(membership=membership)
        self.fields["assigned_to"].queryset = members
        self.fields["helpers"].queryset = members

    def clean(self):
        cleaned = super().clean()
        asset, location = cleaned.get("asset"), cleaned.get("location")
        if asset and location:
            self.add_error("location", _("Elige un activo o una ubicación, no ambos."))
        elif not asset and not location and not self.has_error("asset") and not self.has_error("location"):
            self.add_error("asset", _("Elige el activo o, si no aplica, la ubicación."))
        return cleaned


class AssigneesForm(forms.Form):
    """Responsable principal y de apoyo de una OT existente (WO-01 CA2)."""

    assigned_to = forms.ModelChoiceField(
        label=_("Responsable"), queryset=User.objects.none(), required=False, empty_label=_("Sin asignar")
    )
    helpers = HelpersField()

    def __init__(self, *args, membership, **kwargs):
        super().__init__(*args, **kwargs)
        members = assignable_members(membership=membership)
        self.fields["assigned_to"].queryset = members
        self.fields["helpers"].queryset = members


class ApplyChecklistForm(forms.Form):
    """Elegir un plan publicado cuyo checklist se copia a la OT (sugeridos primero)."""

    plan_version = forms.ChoiceField(label=_("Plan"))

    def __init__(self, *args, versions, **kwargs):
        super().__init__(*args, **kwargs)
        self.versions = {str(v.pk): v for v in versions}

        def option(version):
            label = _("%(plan)s · v%(number)s (%(count)s ítems)") % {
                "plan": version.plan.name,
                "number": version.number,
                "count": version.item_count,
            }
            return (str(version.pk), label)

        suggested = [option(v) for v in versions if v.suggested]
        others = [option(v) for v in versions if not v.suggested]
        choices = [("", _("Elige un plan"))]
        if suggested:
            choices.append((_("Sugeridos para este activo"), suggested))
        if others:
            choices.append((_("Otros planes") if suggested else _("Planes"), others))
        self.fields["plan_version"].choices = choices

    def clean_plan_version(self):
        return self.versions[self.cleaned_data["plan_version"]]


class ChecklistItemForm(forms.Form):
    """Ítem propio agregado a una OT no preventiva."""

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


class ChecklistPhotoForm(forms.Form):
    photo = forms.FileField(label=_("Foto"))
    # Generado al mostrar el formulario: un reintento no duplica la foto.
    client_uuid = forms.UUIDField(widget=forms.HiddenInput, required=False)
