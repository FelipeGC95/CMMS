import secrets

from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.core.models import TenantModel


def new_qr_token():
    """Token no adivinable del QR (nunca el ID interno). Ver AST-05."""
    return secrets.token_urlsafe(12)


class AssetClass(TenantModel):
    class IsoCategory(models.TextChoices):
        """Categoría de referencia ISO 14224 (AST-01 CA2)."""

        ROTATING = "rotating", _("Rotativo")
        MECHANICAL = "mechanical", _("Mecánico")
        ELECTRICAL = "electrical", _("Eléctrico")
        INSTRUMENTATION = "instrumentation", _("Instrumentación")
        VEHICLE = "vehicle", _("Vehículo")
        SAFETY = "safety", _("Seguridad")
        OTHER = "other", _("Otro")

    class Level(models.TextChoices):
        EQUIPMENT = "equipment", _("Equipo")
        SYSTEM = "system", _("Sistema")
        COMPONENT = "component", _("Componente")

    code = models.CharField(_("código"), max_length=30)
    name = models.CharField(_("nombre"), max_length=120)
    iso_category = models.CharField(
        _("categoría ISO 14224"),
        max_length=20,
        choices=IsoCategory.choices,
        default=IsoCategory.OTHER,
        blank=True,
    )
    level_hint = models.CharField(
        _("nivel típico"), max_length=20, choices=Level.choices, default=Level.EQUIPMENT, blank=True
    )
    is_active = models.BooleanField(_("activa"), default=True)

    class Meta:
        verbose_name = _("clase de activo")
        verbose_name_plural = _("clases de activo")
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(fields=["organization", "code"], name="uniq_asset_class_code"),
        ]

    def __str__(self):
        return self.name


class AttributeType(models.TextChoices):
    TEXT = "text", _("Texto")
    NUMBER = "number", _("Número")
    DATE = "date", _("Fecha")
    BOOL = "bool", _("Sí / No")
    CHOICE = "choice", _("Lista")


class AssetClassAttribute(TenantModel):
    """Atributo técnico definido en la clase (AST-01); el valor vive en `Asset.attributes[key]`.

    `key` y `data_type` no cambian tras crearse: así los valores guardados siguen
    siendo válidos. Desactivar oculta el atributo sin borrar valores (CA3).
    """

    asset_class = models.ForeignKey(
        AssetClass, on_delete=models.CASCADE, related_name="attributes", verbose_name=_("clase")
    )
    key = models.SlugField(_("clave"), max_length=50, editable=False)
    label = models.CharField(_("nombre"), max_length=80)
    data_type = models.CharField(_("tipo"), max_length=10, choices=AttributeType.choices)
    unit = models.CharField(_("unidad"), max_length=20, blank=True)
    choices = models.JSONField(_("opciones"), default=list, blank=True)
    required = models.BooleanField(_("obligatorio"), default=False)
    order = models.PositiveSmallIntegerField(_("orden"), default=0)
    is_active = models.BooleanField(_("activo"), default=True)

    class Meta:
        verbose_name = _("atributo de clase")
        verbose_name_plural = _("atributos de clase")
        ordering = ["order", "label"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "asset_class", "key"], name="uniq_asset_class_attribute_key"
            ),
        ]

    def __str__(self):
        return f"{self.label} ({self.unit})" if self.unit else self.label


class Asset(TenantModel):
    class Status(models.TextChoices):
        OPERATIONAL = "operational", _("Operativo")
        STOPPED = "stopped", _("Detenido")
        IN_MAINTENANCE = "in_maintenance", _("En mantención")
        RETIRED = "retired", _("De baja")

    class Criticality(models.TextChoices):
        A = "A", _("A · Alta")
        B = "B", _("B · Media")
        C = "C", _("C · Baja")

    code = models.CharField(_("código"), max_length=30)
    name = models.CharField(_("nombre"), max_length=150)
    asset_class = models.ForeignKey(
        AssetClass, on_delete=models.PROTECT, related_name="assets", verbose_name=_("clase")
    )
    location = models.ForeignKey(
        "organizations.Location",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="assets",
        verbose_name=_("ubicación"),
    )
    parent = models.ForeignKey(
        "self",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="children",
        verbose_name=_("activo padre"),
    )
    status = models.CharField(_("estado"), max_length=20, choices=Status.choices, default=Status.OPERATIONAL)
    criticality = models.CharField(
        _("criticidad"), max_length=1, choices=Criticality.choices, default=Criticality.B
    )
    manufacturer = models.CharField(_("fabricante"), max_length=100, blank=True)
    model = models.CharField(_("modelo"), max_length=100, blank=True)
    serial_number = models.CharField(_("n.º de serie"), max_length=100, blank=True)
    year = models.PositiveSmallIntegerField(_("año"), null=True, blank=True)
    in_service_on = models.DateField(_("fecha de puesta en servicio"), null=True, blank=True)
    # Valores de los atributos de la clase por `key` (AST-01); JSON serializable (número y fecha como texto).
    attributes = models.JSONField(_("atributos"), default=dict, blank=True)
    qr_token = models.CharField(
        max_length=32, unique=True, default=new_qr_token, editable=False, verbose_name=_("token QR")
    )
    is_active = models.BooleanField(_("activo"), default=True)

    class Meta:
        verbose_name = _("activo")
        verbose_name_plural = _("activos")
        ordering = ["code"]
        constraints = [
            models.UniqueConstraint(fields=["organization", "code"], name="uniq_asset_code"),
        ]
        indexes = [
            models.Index(fields=["organization", "status"]),
            models.Index(fields=["organization", "location"]),
        ]

    def __str__(self):
        return f"{self.code} · {self.name}"


# Estados que se eligen a mano (AST-04); "de baja" tiene su propio flujo con confirmación.
MANUAL_STATUSES = (Asset.Status.OPERATIONAL, Asset.Status.STOPPED, Asset.Status.IN_MAINTENANCE)


class AssetStatusPeriod(TenantModel):
    """Período continuo en un estado. Base del cálculo de disponibilidad."""

    asset = models.ForeignKey(
        Asset, on_delete=models.CASCADE, related_name="status_periods", verbose_name=_("activo")
    )
    status = models.CharField(max_length=20, choices=Asset.Status.choices, verbose_name=_("estado"))
    started_at = models.DateTimeField(verbose_name=_("inicio"))
    ended_at = models.DateTimeField(null=True, blank=True, verbose_name=_("término"))
    reason = models.CharField(max_length=200, blank=True, verbose_name=_("motivo"))
    # OT y falla que explican el cambio (AST-04 CA2); opcionales en cambios manuales.
    work_order = models.ForeignKey(
        "work.WorkOrder",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("OT"),
    )
    failure_event = models.ForeignKey(
        "work.FailureEvent",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("falla"),
    )
    changed_by = models.ForeignKey(
        "accounts.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("cambiado por"),
    )

    class Meta:
        verbose_name = _("período de estado")
        verbose_name_plural = _("períodos de estado")
        ordering = ["-started_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["asset"],
                condition=models.Q(ended_at__isnull=True),
                name="uniq_open_status_period",
            ),
        ]
        indexes = [models.Index(fields=["organization", "asset", "started_at"])]


class MeterKind(models.TextChoices):
    HOURS = "hours", _("Horómetro (h)")
    KM = "km", _("Odómetro (km)")
    CYCLES = "cycles", _("Ciclos")
    OTHER = "other", _("Otro")


METER_DEFAULT_UNITS = {MeterKind.HOURS: "h", MeterKind.KM: "km", MeterKind.CYCLES: "ciclos"}


class Meter(TenantModel):
    """Medidor de uso de un activo (AST-07)."""

    asset = models.ForeignKey(
        Asset, on_delete=models.CASCADE, related_name="meters", verbose_name=_("activo")
    )
    kind = models.CharField(_("tipo"), max_length=10, choices=MeterKind.choices)
    unit = models.CharField(_("unidad"), max_length=20, blank=True)
    name = models.CharField(_("nombre"), max_length=80, blank=True)
    is_cumulative = models.BooleanField(_("acumulativo"), default=True)
    current_value = models.DecimalField(
        _("lectura actual"), max_digits=14, decimal_places=3, null=True, blank=True, editable=False
    )
    current_read_at = models.DateTimeField(
        null=True, blank=True, editable=False, verbose_name=_("fecha de la lectura actual")
    )
    # Uso diario promedio (últimos 30 días): proyecta vencimientos por uso.
    avg_daily_usage = models.DecimalField(
        max_digits=14,
        decimal_places=3,
        null=True,
        blank=True,
        editable=False,
        verbose_name=_("uso diario promedio"),
    )
    is_active = models.BooleanField(_("activo"), default=True)

    class Meta:
        verbose_name = _("medidor")
        verbose_name_plural = _("medidores")
        ordering = ["kind"]
        constraints = [
            models.UniqueConstraint(fields=["asset", "kind", "name"], name="uniq_meter_per_asset"),
        ]

    def __str__(self):
        return f"{self.name or self.get_kind_display()} · {self.asset.code}"

    def save(self, *args, **kwargs):
        if not self.unit:
            self.unit = METER_DEFAULT_UNITS.get(self.kind, "")
        super().save(*args, **kwargs)


class MeterReading(TenantModel):
    class Source(models.TextChoices):
        MANUAL = "manual", _("Manual")
        IMPORT = "import", _("Importación")
        API = "api", _("API")
        WORK_ORDER = "work_order", _("Cierre de OT")

    meter = models.ForeignKey(
        Meter, on_delete=models.CASCADE, related_name="readings", verbose_name=_("medidor")
    )
    value = models.DecimalField(_("lectura"), max_digits=14, decimal_places=3)
    read_at = models.DateTimeField(_("fecha de lectura"))
    source = models.CharField(
        max_length=20, choices=Source.choices, default=Source.MANUAL, verbose_name=_("origen")
    )
    is_reset = models.BooleanField(_("reinicio o cambio de medidor"), default=False)
    work_order = models.ForeignKey(
        "work.WorkOrder",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("OT"),
    )
    created_by = models.ForeignKey(
        "accounts.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("registrada por"),
    )
    client_uuid = models.UUIDField(null=True, blank=True, verbose_name=_("UUID del cliente"))

    class Meta:
        verbose_name = _("lectura de medidor")
        verbose_name_plural = _("lecturas de medidor")
        ordering = ["-read_at"]
        indexes = [models.Index(fields=["meter", "-read_at"])]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "client_uuid"], name="uniq_meter_reading_client_uuid"
            ),
        ]
