import uuid
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


class LaborEntry(TenantModel):
    work_order = models.ForeignKey(WorkOrder, on_delete=models.CASCADE, related_name="labor")
    user = models.ForeignKey("accounts.User", on_delete=models.PROTECT, related_name="+")
    minutes = models.PositiveIntegerField()


class FailureCode(TenantModel):
    """Catálogo con la estructura de ISO 14224 (contenido propio y editable)."""

    class Kind(models.TextChoices):
        MODE = "mode", _("Modo de falla")
        CAUSE = "cause", _("Causa")

    kind = models.CharField(_("tipo"), max_length=10, choices=Kind.choices)
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
    work_order = models.OneToOneField(
        WorkOrder, on_delete=models.SET_NULL, null=True, blank=True, related_name="failure"
    )
    impact = models.CharField(max_length=20, choices=Impact.choices)
    description = models.TextField(blank=True)
    failure_mode = models.ForeignKey(
        FailureCode, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    failure_cause = models.ForeignKey(
        FailureCode, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    started_at = models.DateTimeField()
    ended_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-started_at"]
        indexes = [models.Index(fields=["organization", "asset", "started_at"])]


class WorkRequest(TenantModel):
    """Aviso de falla o solicitud de trabajo (REQ-01).

    En la demo se acepta automáticamente y genera una OT correctiva: el flujo
    por defecto es el automático.
    """

    number = models.PositiveIntegerField(editable=False)
    asset = models.ForeignKey("assets.Asset", on_delete=models.PROTECT, related_name="requests")
    description = models.TextField()
    asset_stopped = models.BooleanField(default=False)
    requested_by = models.ForeignKey("accounts.User", on_delete=models.SET_NULL, null=True, related_name="+")
    work_order = models.OneToOneField(
        WorkOrder, on_delete=models.SET_NULL, null=True, blank=True, related_name="request"
    )
    client_uuid = models.UUIDField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["organization", "client_uuid"], name="uniq_request_client_uuid"),
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
