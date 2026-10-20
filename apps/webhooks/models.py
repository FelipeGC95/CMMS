"""Webhooks salientes (X-08, ADR-030): destinos por organización y registro de entregas."""

from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.core.models import TenantModel


class Event(models.TextChoices):
    WORK_ORDER_CREATED = "work_order.created", _("OT creada")
    WORK_ORDER_STATUS_CHANGED = "work_order.status_changed", _("OT cambió de estado")
    WORK_ORDER_COMPLETED = "work_order.completed", _("OT completada")
    WORK_REQUEST_CREATED = "work_request.created", _("Solicitud creada")
    ASSET_CREATED = "asset.created", _("Activo creado")
    ASSET_UPDATED = "asset.updated", _("Activo editado")
    ASSET_STATUS_CHANGED = "asset.status_changed", _("Activo cambió de estado")
    METER_READING_CREATED = "meter_reading.created", _("Lectura registrada")
    CONDITION_ALERT_OPENED = "condition_alert.opened", _("Alerta de condición abierta")
    CONDITION_ALERT_ACKNOWLEDGED = "condition_alert.acknowledged", _("Alerta de condición reconocida")
    CONDITION_ALERT_NORMALIZED = "condition_alert.normalized", _("Alerta de condición normalizada")
    CONDITION_ALERT_CLOSED = "condition_alert.closed", _("Alerta de condición cerrada")
    PART_BELOW_MINIMUM = "part.below_minimum", _("Repuesto bajo mínimo")
    STOCK_MOVEMENT_CREATED = "stock_movement.created", _("Movimiento de stock")


# Módulo del plan que necesita cada evento, además de `api` (sin él no se ofrece ni se envía).
EVENT_MODULES = {
    Event.WORK_ORDER_CREATED: "work_orders",
    Event.WORK_ORDER_STATUS_CHANGED: "work_orders",
    Event.WORK_ORDER_COMPLETED: "work_orders",
    Event.WORK_REQUEST_CREATED: "requests",
    Event.ASSET_CREATED: "assets",
    Event.ASSET_UPDATED: "assets",
    Event.ASSET_STATUS_CHANGED: "assets",
    Event.METER_READING_CREATED: "assets",
    Event.CONDITION_ALERT_OPENED: "condition",
    Event.CONDITION_ALERT_ACKNOWLEDGED: "condition",
    Event.CONDITION_ALERT_NORMALIZED: "condition",
    Event.CONDITION_ALERT_CLOSED: "condition",
    Event.PART_BELOW_MINIMUM: "inventory",
    Event.STOCK_MOVEMENT_CREATED: "inventory",
}


# Evento de "Enviar prueba": no se suscribe, solo se manda a pedido a un destino.
TEST_EVENT = "webhook.test"
# Lo que se guarda de la respuesta del receptor (para el detalle de la entrega).
RESPONSE_BODY_LIMIT = 1000


class WebhookEndpoint(TenantModel):
    """URL https de otro sistema que recibe los eventos suscritos, firmados con `secret`.

    El secreto se guarda en claro (se necesita para firmar); se muestra al crearlo o
    regenerarlo y solo a quien administra integraciones. Sin borrado físico: se desactiva.
    """

    name = models.CharField(_("nombre"), max_length=80)
    url = models.URLField(_("URL"), max_length=500)
    secret = models.CharField(_("secreto"), max_length=80, editable=False)
    events = models.JSONField(_("eventos"), default=list)
    is_active = models.BooleanField(_("activo"), default=True)
    secret_rotated_at = models.DateTimeField(_("secreto regenerado"), null=True, blank=True)
    consecutive_failures = models.PositiveIntegerField(_("entregas fallidas seguidas"), default=0)
    disabled_at = models.DateTimeField(_("desactivado automáticamente"), null=True, blank=True)
    created_by = models.ForeignKey(
        "accounts.User",
        on_delete=models.SET_NULL,
        null=True,
        related_name="+",
        verbose_name=_("creado por"),
    )

    class Meta:
        verbose_name = _("webhook")
        verbose_name_plural = _("webhooks")
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["organization", "-created_at"], name="webhook_org_created")]

    def __str__(self):
        return self.name

    def event_labels(self) -> list[str]:
        labels = dict(Event.choices)
        return [str(labels.get(code, code)) for code in self.events]


class WebhookDelivery(TenantModel):
    """Un evento enviado (o por enviar) a un destino. Los reintentos reutilizan la fila.

    Reenviar a mano crea otra fila con el mismo `event_id` (idempotencia del receptor) y
    `redelivery_of` apuntando a la original. Las de más de 90 días se purgan (docs/07).
    """

    class Status(models.TextChoices):
        PENDING = "pending", _("Pendiente")
        SUCCEEDED = "succeeded", _("Entregado")
        FAILED = "failed", _("Fallido")

    endpoint = models.ForeignKey(
        WebhookEndpoint, on_delete=models.PROTECT, related_name="deliveries", verbose_name=_("webhook")
    )
    event_id = models.UUIDField(_("ID del evento"))
    event_type = models.CharField(_("evento"), max_length=40)
    payload = models.JSONField(_("contenido"))
    status = models.CharField(_("estado"), max_length=12, choices=Status.choices, default=Status.PENDING)
    attempts = models.PositiveSmallIntegerField(_("intentos"), default=0)
    response_status = models.PositiveSmallIntegerField(_("código HTTP"), null=True, blank=True)
    response_body = models.CharField(_("respuesta"), max_length=RESPONSE_BODY_LIMIT, blank=True)
    last_error = models.CharField(_("último error"), max_length=300, blank=True)
    last_attempt_at = models.DateTimeField(_("último intento"), null=True, blank=True)
    next_attempt_at = models.DateTimeField(_("próximo intento"), null=True, blank=True)
    delivered_at = models.DateTimeField(_("entregado"), null=True, blank=True)
    is_redelivery = models.BooleanField(_("reenvío manual"), default=False)
    redelivery_of = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="redeliveries",
        verbose_name=_("reenvío de"),
    )
    created_by = models.ForeignKey(
        "accounts.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("reenviado por"),
    )

    class Meta:
        verbose_name = _("entrega de webhook")
        verbose_name_plural = _("entregas de webhook")
        ordering = ["-created_at"]
        indexes = [
            models.Index(
                fields=["organization", "endpoint", "-created_at"], name="webhook_delivery_endpoint"
            ),
            models.Index(fields=["organization", "created_at"], name="webhook_delivery_created"),
        ]
        constraints = [
            # Un evento llega una vez por destino; los reenvíos manuales repiten el `event_id`.
            models.UniqueConstraint(
                fields=["organization", "endpoint", "event_id"],
                condition=models.Q(is_redelivery=False),
                name="uniq_webhook_delivery_event",
            )
        ]

    def __str__(self):
        return f"{self.event_type} → {self.endpoint_id}"

    @property
    def event_label(self) -> str:
        if self.event_type == TEST_EVENT:
            return str(_("Prueba"))
        return str(dict(Event.choices).get(self.event_type, self.event_type))
