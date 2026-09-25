import uuid
from decimal import Decimal
from pathlib import Path

from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.core.models import TenantModel
from apps.maintenance.models import ItemType


class WorkOrderSequence(TenantModel):
    """Correlativos por organización y tipo, sin huecos por concurrencia."""

    key = models.CharField(max_length=20)
    last_number = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["organization", "key"], name="uniq_sequence_key"),
        ]


class WorkOrder(TenantModel):
    class Type(models.TextChoices):
        PREVENTIVE = "preventive", _("Preventiva")
        CORRECTIVE = "corrective", _("Correctiva")
        PREDICTIVE = "predictive", _("Predictiva / condición")
        IMPROVEMENT = "improvement", _("Mejora")
        INSPECTION = "inspection", _("Inspección")

    class Status(models.TextChoices):
        REQUESTED = "requested", _("Solicitada")
        PLANNED = "planned", _("Planificada")
        SCHEDULED = "scheduled", _("Programada")
        IN_PROGRESS = "in_progress", _("En ejecución")
        ON_HOLD = "on_hold", _("En espera")
        COMPLETED = "completed", _("Completada")
        CLOSED = "closed", _("Cerrada")
        CANCELLED = "cancelled", _("Cancelada")

    class Priority(models.IntegerChoices):
        URGENT = 1, _("Urgente")
        HIGH = 2, _("Alta")
        NORMAL = 3, _("Normal")
        LOW = 4, _("Baja")

    CLOSED_STATUSES = (Status.CLOSED, Status.CANCELLED)
    TYPE_PREFIX = {
        Type.PREVENTIVE: "P",
        Type.CORRECTIVE: "C",
        Type.PREDICTIVE: "D",
        Type.IMPROVEMENT: "M",
        Type.INSPECTION: "I",
    }

    number = models.PositiveIntegerField(_("número"), editable=False)
    work_type = models.CharField(_("tipo"), max_length=20, choices=Type.choices)
    status = models.CharField(_("estado"), max_length=20, choices=Status.choices)
    priority = models.PositiveSmallIntegerField(
        _("prioridad"), choices=Priority.choices, default=Priority.NORMAL
    )
    title = models.CharField(_("título"), max_length=200)
    description = models.TextField(_("descripción"), blank=True)
    asset = models.ForeignKey(
        "assets.Asset", on_delete=models.PROTECT, related_name="work_orders", verbose_name=_("activo")
    )
    plan_assignment = models.ForeignKey(
        "maintenance.PlanAssignment",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="work_orders",
    )
    # Versión del plan con que se generó (inmutable, ADR-016); el checklist va copiado aparte.
    plan_version = models.ForeignKey(
        "maintenance.PlanVersion",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="work_orders",
    )
    assigned_to = models.ForeignKey(
        "accounts.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="assigned_work_orders",
        verbose_name=_("responsable"),
    )
    due_on = models.DateField(_("fecha límite"), null=True, blank=True)
    due_meter_value = models.DecimalField(
        _("vence al llegar a"), max_digits=14, decimal_places=3, null=True, blank=True
    )
    estimated_minutes = models.PositiveIntegerField(_("estimación (min)"), null=True, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    completion_notes = models.TextField(_("comentario de cierre"), blank=True)
    # Totales (WO-04 CA3): se recalculan con cada registro y quedan fijos al cerrar.
    labor_cost = models.DecimalField(_("mano de obra"), max_digits=14, decimal_places=2, default=0)
    parts_cost = models.DecimalField(_("repuestos"), max_digits=14, decimal_places=2, default=0)
    other_cost = models.DecimalField(_("otros costos"), max_digits=14, decimal_places=2, default=0)
    total_cost = models.DecimalField(_("costo total"), max_digits=14, decimal_places=2, default=0)
    meter_value_at_completion = models.DecimalField(
        _("lectura al cerrar"), max_digits=14, decimal_places=3, null=True, blank=True
    )
    client_uuid = models.UUIDField(null=True, blank=True)

    class Meta:
        verbose_name = _("orden de trabajo")
        verbose_name_plural = _("órdenes de trabajo")
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "work_type", "number"], name="uniq_work_order_number"
            ),
            models.UniqueConstraint(
                fields=["organization", "client_uuid"], name="uniq_work_order_client_uuid"
            ),
            # Nunca dos OT abiertas de la misma asignación de plan (PM-05).
            models.UniqueConstraint(
                fields=["plan_assignment"],
                condition=models.Q(plan_assignment__isnull=False)
                & ~models.Q(status__in=["closed", "cancelled"]),
                name="uniq_open_work_order_per_assignment",
            ),
        ]
        indexes = [
            models.Index(fields=["organization", "status", "due_on"]),
            models.Index(fields=["organization", "asset", "completed_at"]),
        ]

    def __str__(self):
        return f"{self.code} · {self.title}"

    @property
    def code(self):
        return f"OT-{self.TYPE_PREFIX[self.work_type]}-{self.number:05d}"

    @property
    def is_open(self):
        return self.status not in self.CLOSED_STATUSES and self.status != self.Status.COMPLETED


class WorkOrderTransition(TenantModel):
    work_order = models.ForeignKey(WorkOrder, on_delete=models.CASCADE, related_name="transitions")
    from_status = models.CharField(max_length=20, blank=True)
    to_status = models.CharField(max_length=20)
    by = models.ForeignKey(
        "accounts.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    comment = models.CharField(max_length=500, blank=True)

    class Meta:
        ordering = ["created_at"]


class WorkOrderChecklistItem(TenantModel):
    """Copia (snapshot) del ítem del plan + resultado de la ejecución."""

    class Result(models.TextChoices):
        OK = "ok", _("OK")
        NOT_OK = "not_ok", _("No OK")
        NA = "na", _("N.A.")

    work_order = models.ForeignKey(WorkOrder, on_delete=models.CASCADE, related_name="checklist")
    order = models.PositiveSmallIntegerField(default=0)
    text = models.CharField(max_length=250)
    item_type = models.CharField(max_length=20, choices=ItemType.choices, default=ItemType.CHECK)
    unit = models.CharField(max_length=20, blank=True)
    min_value = models.DecimalField(max_digits=14, decimal_places=3, null=True, blank=True)
    max_value = models.DecimalField(max_digits=14, decimal_places=3, null=True, blank=True)
    required = models.BooleanField(default=True)
    result = models.CharField(max_length=10, choices=Result.choices, blank=True)
    value_number = models.DecimalField(max_digits=14, decimal_places=3, null=True, blank=True)
    note = models.CharField(max_length=500, blank=True)
    answered_by = models.ForeignKey(
        "accounts.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    answered_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["order"]


MONEY = {"max_digits": 14, "decimal_places": 2}


class LaborRate(TenantModel):
    """Costo por hora de mano de obra (WO-04 CA1).

    Una fila sin membresía es la tarifa general de la organización; una fila
    con membresía la reemplaza para esa persona. Se copia a cada registro de
    horas, así que cambiarla no altera OT pasadas.
    """

    membership = models.OneToOneField(
        "accounts.Membership",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="labor_rate",
        verbose_name=_("persona (vacío = tarifa general)"),
    )
    hourly_cost = models.DecimalField(_("costo por hora"), **MONEY)

    class Meta:
        verbose_name = _("tarifa de mano de obra")
        verbose_name_plural = _("tarifas de mano de obra")
        constraints = [
            models.UniqueConstraint(
                fields=["organization"],
                condition=models.Q(membership__isnull=True),
                name="uniq_default_labor_rate",
            ),
        ]

    def __str__(self):
        who = self.membership.user if self.membership_id else _("Tarifa general")
        return f"{who}: {self.hourly_cost}"


class LaborEntry(TenantModel):
    work_order = models.ForeignKey(WorkOrder, on_delete=models.CASCADE, related_name="labor")
    user = models.ForeignKey("accounts.User", on_delete=models.PROTECT, related_name="+")
    minutes = models.PositiveIntegerField()
    # Copia de la tarifa vigente al registrar (WO-04 CA1).
    hourly_cost = models.DecimalField(_("costo por hora"), default=0, **MONEY)

    class Meta:
        ordering = ["created_at"]

    @property
    def cost(self):
        return (self.hourly_cost * self.minutes / 60).quantize(Decimal("0.01"))


class PartUsage(TenantModel):
    """Repuesto o material consumido en la OT (WO-04 CA2; catálogo en F2)."""

    work_order = models.ForeignKey(WorkOrder, on_delete=models.CASCADE, related_name="parts")
    description = models.CharField(_("repuesto o material"), max_length=200)
    quantity = models.DecimalField(_("cantidad"), max_digits=12, decimal_places=3)
    unit = models.CharField(_("unidad"), max_length=20, blank=True)
    unit_cost = models.DecimalField(_("costo unitario"), **MONEY)
    created_by = models.ForeignKey(
        "accounts.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    client_uuid = models.UUIDField(null=True, blank=True)

    class Meta:
        ordering = ["created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "client_uuid"], name="uniq_part_usage_client_uuid"
            ),
        ]

    @property
    def cost(self):
        return (self.quantity * self.unit_cost).quantize(Decimal("0.01"))


class OtherCost(TenantModel):
    """Otro costo de la OT, por ejemplo un servicio externo (WO-04 CA2)."""

    work_order = models.ForeignKey(WorkOrder, on_delete=models.CASCADE, related_name="other_costs")
    description = models.CharField(_("descripción"), max_length=200)
    supplier = models.CharField(_("proveedor"), max_length=150, blank=True)
    amount = models.DecimalField(_("monto"), **MONEY)
    created_by = models.ForeignKey(
        "accounts.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )

    class Meta:
        ordering = ["created_at"]

    @property
    def cost(self):
        return self.amount


class FailureCode(TenantModel):
    """Catálogo con la estructura de ISO 14224 (contenido propio y editable).

    Sin borrado físico: un código que ya no se usa se desactiva (`is_active`),
    nunca se borra (queda referenciado por eventos de falla históricos).
    """

    class Kind(models.TextChoices):
        MODE = "mode", _("Modo de falla")
        CAUSE = "cause", _("Causa")
        MECHANISM = "mechanism", _("Mecanismo de falla")
        DETECTION_METHOD = "detection_method", _("Método de detección")

    kind = models.CharField(_("tipo"), max_length=20, choices=Kind.choices)
    code = models.CharField(_("código"), max_length=10)
    name = models.CharField(_("nombre"), max_length=120)
    is_active = models.BooleanField(default=True)

    class Meta:
        verbose_name = _("código de falla")
        verbose_name_plural = _("códigos de falla")
        ordering = ["kind", "name"]
        constraints = [
            models.UniqueConstraint(fields=["organization", "kind", "code"], name="uniq_failure_code"),
        ]

    def __str__(self):
        return f"{self.code} · {self.name}"


class FailureEvent(TenantModel):
    class Impact(models.TextChoices):
        TOTAL_STOP = "total_stop", _("Detención total")
        PARTIAL = "partial", _("Detención parcial")
        NONE = "none", _("Sin detención")

    asset = models.ForeignKey("assets.Asset", on_delete=models.PROTECT, related_name="failures")
    # Activo hijo afectado (opcional): ej. un motor dentro de una envasadora.
    component = models.ForeignKey(
        "assets.Asset",
        verbose_name=_("componente"),
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
    )
    work_order = models.OneToOneField(
        WorkOrder, on_delete=models.SET_NULL, null=True, blank=True, related_name="failure"
    )
    impact = models.CharField(max_length=20, choices=Impact.choices)
    description = models.TextField(blank=True)
    failure_mode = models.ForeignKey(
        FailureCode,
        verbose_name=_("modo de falla"),
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
    )
    failure_cause = models.ForeignKey(
        FailureCode,
        verbose_name=_("causa"),
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
    )
    failure_mechanism = models.ForeignKey(
        FailureCode,
        verbose_name=_("mecanismo de falla"),
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
    )
    detection_method = models.ForeignKey(
        FailureCode,
        verbose_name=_("método de detección"),
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
    )
    started_at = models.DateTimeField()
    ended_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-started_at"]
        indexes = [models.Index(fields=["organization", "asset", "started_at"])]


class WorkRequest(TenantModel):
    """Aviso de falla o solicitud de trabajo (REQ-01).

    La OT correctiva se genera de inmediato al avisar (CA1/CA2): el flujo por
    defecto es el automático. `status` es la decisión del planificador sobre
    la solicitud en sí (CA3) — aceptarla es solo formalizar lo ya creado;
    rechazarla o fusionarla cancela esa OT (ver `services.reject_request` y
    `services.merge_request`).
    """

    class Status(models.TextChoices):
        PENDING = "pending", _("Pendiente")
        ACCEPTED = "accepted", _("Aceptada")
        REJECTED = "rejected", _("Rechazada")
        MERGED = "merged", _("Fusionada")

    number = models.PositiveIntegerField(editable=False)
    asset = models.ForeignKey("assets.Asset", on_delete=models.PROTECT, related_name="requests")
    description = models.TextField()
    asset_stopped = models.BooleanField(default=False)
    requested_by = models.ForeignKey("accounts.User", on_delete=models.SET_NULL, null=True, related_name="+")
    work_order = models.OneToOneField(
        WorkOrder, on_delete=models.SET_NULL, null=True, blank=True, related_name="request"
    )
    status = models.CharField(_("estado"), max_length=10, choices=Status.choices, default=Status.PENDING)
    decided_by = models.ForeignKey(
        "accounts.User",
        verbose_name=_("decidida por"),
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    decided_at = models.DateTimeField(_("decidida el"), null=True, blank=True)
    rejection_reason = models.CharField(_("motivo de rechazo"), max_length=500, blank=True)
    merged_into = models.ForeignKey(
        "self",
        verbose_name=_("fusionada en"),
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="merged_requests",
    )
    client_uuid = models.UUIDField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["organization", "client_uuid"], name="uniq_request_client_uuid"),
        ]
        indexes = [
            models.Index(fields=["organization", "status", "created_at"], name="request_org_status_idx"),
        ]


def attachment_upload_to(instance, filename):
    ext = Path(filename).suffix.lower()[:10]
    return f"org/{instance.organization_id}/{uuid.uuid4().hex}{ext}"


class Attachment(TenantModel):
    work_order = models.ForeignKey(WorkOrder, on_delete=models.CASCADE, related_name="attachments")
    file = models.FileField(upload_to=attachment_upload_to)
    filename = models.CharField(max_length=200)
    content_type = models.CharField(max_length=100)
    size_bytes = models.PositiveIntegerField()
    uploaded_by = models.ForeignKey("accounts.User", on_delete=models.SET_NULL, null=True, related_name="+")

    class Meta:
        ordering = ["created_at"]
