import calendar
import datetime
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils.translation import gettext_lazy as _
from django.utils.translation import ngettext

from apps.assets.models import METER_DEFAULT_UNITS, MeterKind
from apps.core.models import TenantManager, TenantModel


class ImmutableVersionError(ValidationError):
    pass


class ItemType(models.TextChoices):
    CHECK = "check", _("Verificación (OK / No OK / N.A.)")
    MEASUREMENT = "measurement", _("Medición")
    TEXT = "text", _("Texto")
    PHOTO = "photo", _("Foto obligatoria")
    SIGNATURE = "signature", _("Firma")


# Tipos que se responden OK / No OK (y pueden originar una correctiva, WO-06).
RESULT_ITEM_TYPES = (ItemType.CHECK, ItemType.MEASUREMENT)


class CalendarUnit(models.TextChoices):
    """Unidad de la frecuencia por calendario (PM-02 CA1)."""

    DAYS = "days", _("días")
    WEEKS = "weeks", _("semanas")
    MONTHS = "months", _("meses")


def add_months(date: datetime.date, months: int) -> datetime.date:
    """Meses de calendario: mismo día del mes, ajustado al último día si no existe.

    31-ene + 1 mes = 28-feb (29 en año bisiesto); 29-feb + 12 meses = 28-feb.
    """
    index = date.month - 1 + months
    year, month = date.year + index // 12, index % 12 + 1
    return date.replace(year=year, month=month, day=min(date.day, calendar.monthrange(year, month)[1]))


def with_anchor_day(date: datetime.date, anchor_day: int | None) -> datetime.date:
    """`date` llevado al día ancla del mes (o al último día si el mes es más corto)."""
    if not anchor_day:
        return date
    return date.replace(day=min(anchor_day, calendar.monthrange(date.year, date.month)[1]))


def shift_date(date: datetime.date, amount: int, unit: str) -> datetime.date:
    """`date` + `amount` unidades (negativo resta). Semanas = 7 días; meses de calendario."""
    if unit == CalendarUnit.MONTHS:
        return add_months(date, amount)
    return date + datetime.timedelta(days=amount * (7 if unit == CalendarUnit.WEEKS else 1))


def format_number(value) -> str:
    """Número en formato es-CL: punto de miles y coma decimal, sin ceros de sobra (10.000; 2,5)."""
    value = Decimal(value).normalize()
    sign = "-" if value < 0 else ""
    integer, _sep, fraction = f"{abs(value):f}".partition(".")
    integer = f"{int(integer):,}".replace(",", ".")
    return f"{sign}{integer},{fraction}" if fraction else f"{sign}{integer}"


DEFAULT_HORIZON_DAYS = 14
MAX_HORIZON_DAYS = 90


class MaintenanceSettings(TenantModel):
    """Ajustes del programador de preventivos de la organización (PM-05 CA1).

    Uno por organización, creado al guardarlo por primera vez; mientras no
    exista rigen los valores por defecto (`maintenance_settings()`).
    """

    horizon_days = models.PositiveSmallIntegerField(
        _("horizonte de generación (días)"),
        default=DEFAULT_HORIZON_DAYS,
        validators=[MinValueValidator(1), MaxValueValidator(MAX_HORIZON_DAYS)],
        help_text=_("Las OT preventivas se generan cuando vencen dentro de estos días."),
    )

    class Meta:
        verbose_name = _("ajustes de preventivos")
        verbose_name_plural = _("ajustes de preventivos")
        constraints = [
            models.UniqueConstraint(fields=["organization"], name="uniq_maintenance_settings"),
        ]

    def __str__(self):
        return str(self._meta.verbose_name)


class MaintenancePlanManager(TenantManager):
    """Trae siempre la versión vigente: las propiedades del plan la leen sin otra consulta.

    Ojo: al llegar al plan por una relación (ej. assignment.plan) Django usa el
    manager base; ahí hay que pedir `select_related("plan__current_version")`.
    """

    def get_queryset(self):
        return super().get_queryset().select_related("current_version")


class MaintenancePlan(TenantModel):
    """Plan preventivo. Su contenido (frecuencia, checklist, recursos) vive en
    versiones inmutables; `current_version` es la publicada vigente (ADR-016).

    Las propiedades de frecuencia delegan en la versión vigente y devuelven
    vacío si el plan aún no tiene versión publicada.
    """

    code = models.CharField(_("código"), max_length=30)
    name = models.CharField(_("nombre"), max_length=150)
    asset_class = models.ForeignKey(
        "assets.AssetClass",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="plans",
        verbose_name=_("clase de activo"),
    )
    is_active = models.BooleanField(_("activo"), default=True)
    current_version = models.ForeignKey(
        "PlanVersion",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
        editable=False,
        verbose_name=_("versión vigente"),
    )

    objects = MaintenancePlanManager()

    class Meta:
        verbose_name = _("plan preventivo")
        verbose_name_plural = _("planes preventivos")
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(fields=["organization", "code"], name="uniq_plan_code"),
        ]

    def __str__(self):
        return self.name

    def _current(self, attr, default=None):
        version = self.current_version
        return getattr(version, attr) if version is not None else default

    @property
    def calendar_interval(self):
        return self._current("calendar_interval")

    @property
    def calendar_unit(self):
        return self._current("calendar_unit", "")

    @property
    def meter_kind(self):
        return self._current("meter_kind", "")

    @property
    def meter_interval(self):
        return self._current("meter_interval")

    @property
    def estimated_minutes(self):
        return self._current("estimated_minutes")

    @property
    def uses_meter(self):
        return self._current("uses_meter", False)

    @property
    def frequency_label(self):
        return self._current("frequency_label", "")

    def get_meter_kind_display(self):
        return self._current("get_meter_kind_display")() if self.current_version else ""

    @property
    def items(self):
        """Checklist de la versión vigente (vacío si no hay versión publicada)."""
        if self.current_version is None:
            return PlanTaskItem.objects.none()
        return self.current_version.items


class DraftContentMixin:
    """Ítems y recursos solo se crean, cambian o borran en versiones borrador."""

    def _check_draft(self):
        version = PlanVersion.all_tenants.filter(pk=self.version_id).only("status", "number").first()
        if version is not None and not version.is_draft:
            raise ImmutableVersionError(
                _("La versión %(number)s está publicada: crea un borrador para cambiar su contenido.")
                % {"number": version.number}
            )

    def save(self, *args, **kwargs):
        self._check_draft()
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        self._check_draft()
        return super().delete(*args, **kwargs)


class PlanVersion(TenantModel):
    class Status(models.TextChoices):
        DRAFT = "draft", _("Borrador")
        PUBLISHED = "published", _("Publicada")
        SUPERSEDED = "superseded", _("Reemplazada")

    class ScheduleMode(models.TextChoices):
        FLOATING = "floating", _("Flotante (desde la ejecución real)")
        FIXED = "fixed", _("Fijo (desde lo programado)")

    plan = models.ForeignKey(
        MaintenancePlan, on_delete=models.CASCADE, related_name="versions", verbose_name=_("plan")
    )
    number = models.PositiveIntegerField(_("versión"))
    status = models.CharField(_("estado"), max_length=12, choices=Status.choices, default=Status.DRAFT)
    published_at = models.DateTimeField(_("publicada el"), null=True, blank=True)
    published_by = models.ForeignKey(
        "accounts.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("publicada por"),
    )
    change_note = models.CharField(_("qué cambió"), max_length=300, blank=True)
    estimated_minutes = models.PositiveIntegerField(_("duración estimada (min)"), default=60)
    instructions = models.TextField(_("instrucciones"), blank=True)
    # Disparador por calendario: cada `calendar_interval` días, semanas o meses (PM-02 CA1).
    calendar_interval = models.PositiveIntegerField(_("cada cuánto (calendario)"), null=True, blank=True)
    calendar_unit = models.CharField(
        _("unidad"), max_length=8, choices=CalendarUnit.choices, default=CalendarUnit.DAYS
    )
    meter_kind = models.CharField(_("medidor"), max_length=10, choices=MeterKind.choices, blank=True)
    meter_interval = models.DecimalField(
        _("cada cuántas unidades del medidor"), max_digits=14, decimal_places=3, null=True, blank=True
    )
    schedule_mode = models.CharField(
        _("reprogramación"), max_length=10, choices=ScheduleMode.choices, default=ScheduleMode.FLOATING
    )
    tolerance_pct = models.PositiveSmallIntegerField(_("tolerancia de cumplimiento (%)"), default=10)

    class Meta:
        verbose_name = _("versión de plan")
        verbose_name_plural = _("versiones de plan")
        ordering = ["-number"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "plan", "number"], name="uniq_plan_version_number"
            ),
            models.UniqueConstraint(
                fields=["organization", "plan"],
                condition=models.Q(status="draft"),
                name="uniq_plan_version_draft",
            ),
        ]

    def __str__(self):
        return f"{self.plan} · v{self.number}"

    # Campos que definen el contenido: no cambian una vez publicada la versión.
    CONTENT_FIELDS = (
        "plan_id", "number", "estimated_minutes", "instructions", "calendar_interval", "calendar_unit",
        "meter_kind", "meter_interval", "schedule_mode", "tolerance_pct",
    )  # fmt: skip

    @property
    def is_draft(self):
        return self.status == self.Status.DRAFT

    @property
    def uses_meter(self):
        return bool(self.meter_kind and self.meter_interval)

    def shift(self, date: datetime.date, periods: int = 1) -> datetime.date:
        """`date` + `periods` veces la frecuencia por calendario (negativo resta)."""
        return shift_date(date, periods * self.calendar_interval, self.calendar_unit)

    def calendar_period_days(self, due_on: datetime.date) -> int:
        """Días del ciclo que termina en `due_on` (un mes de calendario no siempre dura lo mismo)."""
        return (due_on - self.shift(due_on, -1)).days

    @property
    def calendar_label(self):
        n = self.calendar_interval
        if not n:
            return ""
        if self.calendar_unit == CalendarUnit.MONTHS:
            return ngettext("%(n)s mes", "%(n)s meses", n) % {"n": n}
        if self.calendar_unit == CalendarUnit.WEEKS:
            return ngettext("%(n)s semana", "%(n)s semanas", n) % {"n": n}
        return ngettext("%(n)s día", "%(n)s días", n) % {"n": n}

    @property
    def frequency_label(self):
        """«10.000 km o 6 meses»: vence lo que ocurra primero (PM-02 CA1)."""
        parts = []
        if self.meter_interval:
            unit = METER_DEFAULT_UNITS.get(self.meter_kind, "")
            parts.append(f"{format_number(self.meter_interval)} {unit}".strip())
        if self.calendar_interval:
            parts.append(self.calendar_label)
        if len(parts) == 2:
            return _("%(meter)s o %(calendar)s") % {"meter": parts[0], "calendar": parts[1]}
        return str(parts[0]) if parts else ""

    def clean(self):
        if not self.calendar_interval and not self.uses_meter:
            raise ValidationError(_("Define una frecuencia por calendario, por medidor o ambas."))
        if bool(self.meter_kind) != bool(self.meter_interval):
            raise ValidationError(_("Para disparar por medidor indica el tipo y la frecuencia."))

    def save(self, *args, **kwargs):
        """Una versión publicada es inmutable: solo puede cambiar de estado (ADR-016).

        No cubre QuerySet.update() ni bulk_*: los servicios no los usan sobre versiones.
        """
        if not self._state.adding:
            stored = PlanVersion.all_tenants.filter(pk=self.pk).values(*self.CONTENT_FIELDS, "status").first()
            if stored and stored["status"] != self.Status.DRAFT:
                changed = [f for f in self.CONTENT_FIELDS if stored[f] != getattr(self, f)]
                if changed:
                    raise ImmutableVersionError(
                        _(
                            "La versión %(number)s de «%(plan)s» está publicada: "
                            "crea un borrador para cambiarla."
                        )
                        % {"number": self.number, "plan": self.plan}
                    )
        super().save(*args, **kwargs)


class PlanResource(DraftContentMixin, TenantModel):
    """Repuesto o herramienta prevista para una versión (PM-01 CA2; catálogo en F2)."""

    class Kind(models.TextChoices):
        PART = "part", _("Repuesto")
        TOOL = "tool", _("Herramienta")

    version = models.ForeignKey(
        PlanVersion, on_delete=models.CASCADE, related_name="resources", verbose_name=_("versión")
    )
    kind = models.CharField(_("tipo"), max_length=10, choices=Kind.choices, default=Kind.PART)
    description = models.CharField(_("descripción"), max_length=200)
    quantity = models.DecimalField(_("cantidad"), max_digits=12, decimal_places=3, default=1)
    unit = models.CharField(_("unidad"), max_length=20, blank=True)
    # Repuesto del catálogo (INV-11). En una versión publicada solo puede pasar
    # de vacío a un repuesto, una vez (`link_published_resource`, excepción al ADR-016).
    part = models.ForeignKey(
        "inventory.Part",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="plan_resources",
        verbose_name=_("repuesto del catálogo"),
    )

    class Meta:
        verbose_name = _("repuesto o herramienta")
        verbose_name_plural = _("repuestos y herramientas")
        ordering = ["kind", "description"]
        indexes = [
            models.Index(
                fields=["organization", "part"],
                condition=models.Q(part__isnull=False),
                name="planresource_org_part_idx",
            )
        ]

    def __str__(self):
        return self.description


class PlanTaskItem(DraftContentMixin, TenantModel):
    version = models.ForeignKey(
        PlanVersion, on_delete=models.CASCADE, related_name="items", verbose_name=_("versión")
    )
    order = models.PositiveSmallIntegerField(_("orden"), default=0)
    text = models.CharField(_("tarea"), max_length=250)
    item_type = models.CharField(_("tipo"), max_length=20, choices=ItemType.choices, default=ItemType.CHECK)
    unit = models.CharField(_("unidad"), max_length=20, blank=True)
    min_value = models.DecimalField(_("mínimo"), max_digits=14, decimal_places=3, null=True, blank=True)
    max_value = models.DecimalField(_("máximo"), max_digits=14, decimal_places=3, null=True, blank=True)
    required = models.BooleanField(_("obligatorio"), default=True)

    class Meta:
        verbose_name = _("ítem de checklist")
        verbose_name_plural = _("checklist")
        ordering = ["order"]

    def __str__(self):
        return self.text


class PlanAbsorption(TenantModel):
    """«El plan mayor absorbe al menor» (PM-05 CA3): ej. la mantención de 500 h incluye la de 250 h.

    Cuando `plan` vence en un activo y `absorbed_plan` vence en el mismo activo
    dentro de la tolerancia de `plan`, se genera una sola OT con los dos
    checklists (secciones rotuladas) y al cerrarla avanzan ambas asignaciones.
    Es un dato del plan (no de la versión): rige desde que se configura.
    """

    plan = models.ForeignKey(
        MaintenancePlan, on_delete=models.CASCADE, related_name="absorptions", verbose_name=_("plan mayor")
    )
    absorbed_plan = models.ForeignKey(
        MaintenancePlan,
        on_delete=models.CASCADE,
        related_name="absorbed_by",
        verbose_name=_("plan absorbido"),
    )

    class Meta:
        verbose_name = _("absorción de plan")
        verbose_name_plural = _("absorciones de planes")
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "plan", "absorbed_plan"], name="uniq_plan_absorption"
            ),
            models.CheckConstraint(
                condition=~models.Q(plan=models.F("absorbed_plan")), name="plan_absorption_not_self"
            ),
        ]

    def __str__(self):
        return f"{self.plan} ⊃ {self.absorbed_plan}"


class PlanAssignment(TenantModel):
    """Plan aplicado a un activo. Guarda el estado del programador."""

    plan = models.ForeignKey(
        MaintenancePlan, on_delete=models.CASCADE, related_name="assignments", verbose_name=_("plan")
    )
    asset = models.ForeignKey(
        "assets.Asset", on_delete=models.CASCADE, related_name="plan_assignments", verbose_name=_("activo")
    )
    meter = models.ForeignKey(
        "assets.Meter",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="plan_assignments",
        verbose_name=_("medidor"),
    )
    next_due_on = models.DateField(_("próximo vencimiento"), null=True, blank=True)
    next_due_meter_value = models.DecimalField(
        _("próximo vencimiento (medidor)"), max_digits=14, decimal_places=3, null=True, blank=True
    )
    # Día del mes del primer vencimiento: en modo fijo por meses, cada vencimiento
    # vuelve a este día aunque el anterior se haya ajustado a un mes corto
    # (31-ene → 28-feb → 31-mar).
    anchor_day = models.PositiveSmallIntegerField(
        _("día ancla"),
        null=True,
        blank=True,
        validators=[MinValueValidator(1), MaxValueValidator(31)],
    )
    last_done_on = models.DateField(_("última ejecución"), null=True, blank=True)
    last_done_meter_value = models.DecimalField(
        max_digits=14,
        decimal_places=3,
        null=True,
        blank=True,
        verbose_name=_("lectura de la última ejecución"),
    )
    is_active = models.BooleanField(_("activa"), default=True)

    class Meta:
        verbose_name = _("asignación de plan")
        verbose_name_plural = _("asignaciones de planes")
        ordering = ["next_due_on"]
        constraints = [
            models.UniqueConstraint(fields=["organization", "plan", "asset"], name="uniq_plan_assignment"),
        ]
        indexes = [models.Index(fields=["organization", "next_due_on"])]

    def __str__(self):
        return f"{self.plan} → {self.asset}"
