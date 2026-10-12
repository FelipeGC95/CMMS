"""Mantenimiento por condición (CBM, ADR-029, docs/03 §2.11).

Las variables son medidores del activo con un tipo de condición (`MeterKind`
temperatura, vibración, presión, corriente u otra). Una regla (`ConditionRule`)
dice cuándo una lectura está fuera de rango; el estado por regla y medidor
(`ConditionState`) da la histéresis: se alerta solo al pasar de normal a
alarma. Cada episodio deja una alerta (`ConditionAlert`) y, si la regla lo pide,
una OT predictiva.
"""

from django.conf import settings
from django.db import models
from django.db.models import F, Q
from django.utils.translation import gettext_lazy as _

from apps.assets.models import VARIABLE_METER_KIND_CHOICES
from apps.core.models import TenantModel


def _number(value) -> str:
    from apps.maintenance.models import format_number

    return format_number(value) if value is not None else "—"


class ConditionRule(TenantModel):
    """Cuándo una variable está fuera de rango y qué hacer (CBM-03).

    Alcance exclusivo: un medidor concreto, o una clase de activo + tipo + nombre de
    variable (aplica a todos los activos de la clase, también a los que se creen después).
    Sin borrado físico: se desactiva.
    """

    class Operator(models.TextChoices):
        GT = "gt", _("Mayor que")
        LT = "lt", _("Menor que")
        OUTSIDE = "outside", _("Fuera del rango")
        RATE = "rate", _("Cambio por día mayor que")

    class Action(models.TextChoices):
        ALERT = "alert", _("Solo alertar")
        ALERT_WORK_ORDER = "alert_work_order", _("Alertar y crear OT predictiva")

    class Priority(models.IntegerChoices):  # mismos valores que WorkOrder.Priority
        URGENT = 1, _("Urgente")
        HIGH = 2, _("Alta")
        NORMAL = 3, _("Normal")
        LOW = 4, _("Baja")

    name = models.CharField(_("nombre"), max_length=120, blank=True)
    meter = models.ForeignKey(
        "assets.Meter",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="condition_rules",
        verbose_name=_("variable"),
    )
    asset_class = models.ForeignKey(
        "assets.AssetClass",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="condition_rules",
        verbose_name=_("clase de activo"),
    )
    meter_kind = models.CharField(
        _("tipo de variable"), max_length=20, choices=VARIABLE_METER_KIND_CHOICES, blank=True
    )
    meter_name = models.CharField(_("nombre de la variable"), max_length=80, blank=True)
    operator = models.CharField(_("condición"), max_length=10, choices=Operator.choices)
    threshold = models.DecimalField(_("umbral"), max_digits=14, decimal_places=3, null=True, blank=True)
    min_value = models.DecimalField(_("mínimo"), max_digits=14, decimal_places=3, null=True, blank=True)
    max_value = models.DecimalField(_("máximo"), max_digits=14, decimal_places=3, null=True, blank=True)
    consecutive = models.PositiveSmallIntegerField(_("lecturas seguidas fuera de rango"), default=1)
    action = models.CharField(
        _("acción"), max_length=20, choices=Action.choices, default=Action.ALERT_WORK_ORDER
    )
    work_order_title = models.CharField(_("título de la OT"), max_length=200, blank=True)
    # Null: según la criticidad del activo.
    work_order_priority = models.PositiveSmallIntegerField(
        _("prioridad de la OT"), null=True, blank=True, choices=Priority.choices
    )
    work_order_instructions = models.TextField(_("instrucciones de la OT"), blank=True)
    is_active = models.BooleanField(_("activa"), default=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("creada por"),
    )

    class Meta:
        verbose_name = _("regla de condición")
        verbose_name_plural = _("reglas de condición")
        ordering = ["created_at"]
        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(meter__isnull=False, asset_class__isnull=True, meter_kind="", meter_name="")
                    | (Q(meter__isnull=True, asset_class__isnull=False) & ~Q(meter_kind=""))
                ),
                name="condition_rule_scope_exclusive",
            ),
            models.CheckConstraint(condition=Q(consecutive__gte=1), name="condition_rule_consecutive_gte_1"),
            models.CheckConstraint(
                condition=~Q(operator__in=["gt", "lt", "rate"]) | Q(threshold__isnull=False),
                name="condition_rule_threshold_required",
            ),
            models.CheckConstraint(
                condition=~Q(operator="rate") | Q(threshold__gt=0),
                name="condition_rule_rate_positive",
            ),
            models.CheckConstraint(
                condition=~Q(operator="outside")
                | Q(min_value__isnull=False, max_value__isnull=False, min_value__lt=F("max_value")),
                name="condition_rule_outside_range",
            ),
            models.CheckConstraint(
                condition=Q(work_order_priority__isnull=True)
                | Q(work_order_priority__gte=1, work_order_priority__lte=4),
                name="condition_rule_priority_range",
            ),
        ]
        indexes = [
            models.Index(
                fields=["organization", "meter"], condition=Q(is_active=True), name="cond_rule_org_meter_idx"
            ),
            models.Index(
                fields=["organization", "asset_class", "meter_kind", "meter_name"],
                condition=Q(is_active=True),
                name="cond_rule_org_class_idx",
            ),
        ]

    def __str__(self):
        return self.label

    @property
    def is_class_rule(self) -> bool:
        return self.meter_id is None

    @property
    def scope_label(self) -> str:
        """«Temperatura rodamiento · CMP-01» o «Temperatura rodamiento · clase MOT»."""
        if self.meter_id:
            return str(self.meter)
        variable = self.meter_name or self.get_meter_kind_display()
        return str(_("%(variable)s · clase %(cls)s") % {"variable": variable, "cls": self.asset_class.code})

    @property
    def condition_label(self) -> str:
        """«> 80», «< 2», «fuera de 40–80», «cambio > 5/día»."""
        if self.operator == self.Operator.GT:
            return f"> {_number(self.threshold)}"
        if self.operator == self.Operator.LT:
            return f"< {_number(self.threshold)}"
        if self.operator == self.Operator.OUTSIDE:
            return str(
                _("fuera de %(min)s–%(max)s")
                % {"min": _number(self.min_value), "max": _number(self.max_value)}
            )
        return str(_("cambio > %(n)s/día") % {"n": _number(self.threshold)})

    @property
    def label(self) -> str:
        """El nombre, o la descripción calculada si no tiene."""
        return self.name or f"{self.scope_label} {self.condition_label}"


class ConditionState(TenantModel):
    """Estado materializado por (regla, variable): normal o alarma (histéresis, CBM-04)."""

    class Status(models.TextChoices):
        NORMAL = "normal", _("Normal")
        ALARM = "alarm", _("En alarma")

    rule = models.ForeignKey(
        ConditionRule, on_delete=models.PROTECT, related_name="states", verbose_name=_("regla")
    )
    meter = models.ForeignKey(
        "assets.Meter", on_delete=models.PROTECT, related_name="condition_states", verbose_name=_("variable")
    )
    status = models.CharField(_("estado"), max_length=10, choices=Status.choices, default=Status.NORMAL)
    consecutive_count = models.PositiveSmallIntegerField(_("lecturas seguidas fuera de rango"), default=0)
    last_reading = models.ForeignKey(
        "assets.MeterReading",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("última lectura evaluada"),
    )
    last_value = models.DecimalField(
        _("último valor"), max_digits=14, decimal_places=3, null=True, blank=True
    )
    last_evaluated_at = models.DateTimeField(_("fecha de la última lectura evaluada"), null=True, blank=True)
    open_alert = models.ForeignKey(
        "ConditionAlert",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("alerta del episodio en curso"),
    )

    class Meta:
        verbose_name = _("estado de condición")
        verbose_name_plural = _("estados de condición")
        constraints = [
            models.UniqueConstraint(fields=["organization", "rule", "meter"], name="uniq_condition_state"),
        ]
        indexes = [models.Index(fields=["organization", "meter"], name="cond_state_org_meter_idx")]

    def __str__(self):
        return f"{self.rule} · {self.meter}"


class ConditionAlert(TenantModel):
    """Un episodio fuera de rango de una variable (CBM-04/05). Inmutable salvo su estado."""

    class Status(models.TextChoices):
        OPEN = "open", _("Abierta")
        ACKNOWLEDGED = "acknowledged", _("Reconocida")
        NORMALIZED = "normalized", _("Normalizada")
        CLOSED = "closed", _("Cerrada")

    ACTIVE_STATUSES = (Status.OPEN, Status.ACKNOWLEDGED)  # el episodio sigue fuera de rango
    PENDING_STATUSES = (Status.OPEN, Status.ACKNOWLEDGED, Status.NORMALIZED)  # aún no cerrada

    rule = models.ForeignKey(
        ConditionRule, on_delete=models.PROTECT, related_name="alerts", verbose_name=_("regla")
    )
    meter = models.ForeignKey(
        "assets.Meter", on_delete=models.PROTECT, related_name="condition_alerts", verbose_name=_("variable")
    )
    # Copiado del medidor: filtra por activo y sitio sin join.
    asset = models.ForeignKey(
        "assets.Asset", on_delete=models.PROTECT, related_name="condition_alerts", verbose_name=_("activo")
    )
    reading = models.ForeignKey(
        "assets.MeterReading",
        on_delete=models.PROTECT,
        related_name="+",
        verbose_name=_("lectura que la abrió"),
    )
    value = models.DecimalField(_("valor"), max_digits=14, decimal_places=3)
    opened_at = models.DateTimeField(_("abierta el"))  # fecha de la lectura que la abrió
    last_value = models.DecimalField(_("último valor"), max_digits=14, decimal_places=3)
    last_read_at = models.DateTimeField(_("fecha del último valor"))
    status = models.CharField(_("estado"), max_length=20, choices=Status.choices, default=Status.OPEN)
    # Null si la regla solo alerta. No es único: un episodio nuevo con la OT anterior abierta la reutiliza.
    work_order = models.ForeignKey(
        "work.WorkOrder",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="condition_alerts",
        verbose_name=_("OT predictiva"),
    )
    acknowledged_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("reconocida por"),
    )
    acknowledged_at = models.DateTimeField(_("reconocida el"), null=True, blank=True)
    normalized_at = models.DateTimeField(_("normalizada el"), null=True, blank=True)
    closed_at = models.DateTimeField(_("cerrada el"), null=True, blank=True)

    class Meta:
        verbose_name = _("alerta de condición")
        verbose_name_plural = _("alertas de condición")
        ordering = ["-created_at"]
        constraints = [
            # Un solo episodio abierto por regla y variable (la histéresis lo garantiza; esto, la base).
            models.UniqueConstraint(
                fields=["organization", "rule", "meter"],
                condition=Q(status__in=["open", "acknowledged"]),
                name="uniq_condition_alert_active",
            ),
        ]
        indexes = [
            models.Index(fields=["organization", "status", "-created_at"], name="cond_alert_org_status_idx"),
            models.Index(fields=["organization", "asset", "-created_at"], name="cond_alert_org_asset_idx"),
            models.Index(
                fields=["organization", "work_order"],
                condition=Q(work_order__isnull=False),
                name="cond_alert_org_wo_idx",
            ),
        ]

    def __str__(self):
        return f"{self.meter} · {self.get_status_display()}"

    @property
    def is_active(self) -> bool:
        return self.status in self.ACTIVE_STATUSES
