from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.core.models import TenantModel


class ItemType(models.TextChoices):
    CHECK = "check", _("Verificación (OK / No OK / N.A.)")
    MEASUREMENT = "measurement", _("Medición")


class MaintenancePlan(TenantModel):
    """Plan preventivo por calendario.

    Versión mínima de la demo: sin versionado ni disparadores por medidor. Las OT
    copian el checklist al generarse, así que agregar versiones después no
    altera OT existentes (PM-03).
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
    interval_days = models.PositiveIntegerField(_("frecuencia (días)"))
    estimated_minutes = models.PositiveIntegerField(_("duración estimada (min)"), default=60)
    is_active = models.BooleanField(_("activo"), default=True)

    class Meta:
        verbose_name = _("plan preventivo")
        verbose_name_plural = _("planes preventivos")
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(fields=["organization", "code"], name="uniq_plan_code"),
        ]

    def __str__(self):
        return self.name


class PlanTaskItem(TenantModel):
    plan = models.ForeignKey(MaintenancePlan, on_delete=models.CASCADE, related_name="items")
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

    plan = models.ForeignKey(MaintenancePlan, on_delete=models.CASCADE, related_name="assignments")
    asset = models.ForeignKey("assets.Asset", on_delete=models.CASCADE, related_name="plan_assignments")
    next_due_on = models.DateField(_("próximo vencimiento"))
    last_done_on = models.DateField(_("última ejecución"), null=True, blank=True)
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
