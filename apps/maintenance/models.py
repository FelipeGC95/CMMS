from django.core.exceptions import ValidationError
from django.db import models
from django.utils.translation import gettext_lazy as _

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
    def interval_days(self):
        return self._current("interval_days")

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
    interval_days = models.PositiveIntegerField(_("cada cuántos días"), null=True, blank=True)
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
        "plan_id", "number", "estimated_minutes", "instructions", "interval_days",
        "meter_kind", "meter_interval", "schedule_mode", "tolerance_pct",
    )  # fmt: skip

    @property
    def is_draft(self):
        return self.status == self.Status.DRAFT

    @property
    def uses_meter(self):
        return bool(self.meter_kind and self.meter_interval)

    @property
    def frequency_label(self):
        parts = []
        if self.meter_interval:
            unit = METER_DEFAULT_UNITS.get(self.meter_kind, "")
            parts.append(f"{self.meter_interval.normalize():f} {unit}")
        if self.interval_days:
            parts.append(_("%(n)s días") % {"n": self.interval_days})
        return " o ".join(str(p) for p in parts)

    def clean(self):
        if not self.interval_days and not self.uses_meter:
            raise ValidationError(_("Define una frecuencia en días, por medidor o ambas."))
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

    class Meta:
        verbose_name = _("repuesto o herramienta")
        verbose_name_plural = _("repuestos y herramientas")
        ordering = ["kind", "description"]

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
