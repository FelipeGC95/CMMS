import secrets

from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.core.models import TenantModel


def new_qr_token():
    """Token no adivinable del QR (nunca el ID interno). Ver AST-05."""
    return secrets.token_urlsafe(12)


class AssetClass(TenantModel):
    code = models.CharField(_("código"), max_length=30)
    name = models.CharField(_("nombre"), max_length=120)
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
    qr_token = models.CharField(max_length=32, unique=True, default=new_qr_token, editable=False)
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


class AssetStatusPeriod(TenantModel):
    """Período continuo en un estado. Base del cálculo de disponibilidad."""

    asset = models.ForeignKey(Asset, on_delete=models.CASCADE, related_name="status_periods")
    status = models.CharField(max_length=20, choices=Asset.Status.choices)
    started_at = models.DateTimeField()
    ended_at = models.DateTimeField(null=True, blank=True)
    reason = models.CharField(max_length=200, blank=True)
    changed_by = models.ForeignKey(
        "accounts.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )

    class Meta:
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

    asset = models.ForeignKey(Asset, on_delete=models.CASCADE, related_name="meters")
    kind = models.CharField(_("tipo"), max_length=10, choices=MeterKind.choices)
    unit = models.CharField(_("unidad"), max_length=20, blank=True)
    name = models.CharField(_("nombre"), max_length=80, blank=True)
    is_cumulative = models.BooleanField(_("acumulativo"), default=True)
    current_value = models.DecimalField(
        _("lectura actual"), max_digits=14, decimal_places=3, null=True, blank=True, editable=False
    )
    current_read_at = models.DateTimeField(null=True, blank=True, editable=False)
    # Uso diario promedio (últimos 30 días): proyecta vencimientos por uso.
    avg_daily_usage = models.DecimalField(
        max_digits=14, decimal_places=3, null=True, blank=True, editable=False
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

    meter = models.ForeignKey(Meter, on_delete=models.CASCADE, related_name="readings")
    value = models.DecimalField(_("lectura"), max_digits=14, decimal_places=3)
    read_at = models.DateTimeField(_("fecha de lectura"))
    source = models.CharField(max_length=20, choices=Source.choices, default=Source.MANUAL)
    is_reset = models.BooleanField(_("reinicio o cambio de medidor"), default=False)
    work_order = models.ForeignKey(
        "work.WorkOrder", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    created_by = models.ForeignKey(
        "accounts.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    client_uuid = models.UUIDField(null=True, blank=True)

    class Meta:
        ordering = ["-read_at"]
        indexes = [models.Index(fields=["meter", "-read_at"])]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "client_uuid"], name="uniq_meter_reading_client_uuid"
            ),
        ]
