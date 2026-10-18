"""Webhooks salientes (X-08, ADR-030): destinos por organización y registro de entregas."""

from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.core.models import TenantModel


class Event(models.TextChoices):
    WORK_ORDER_CREATED = "work_order.created", _("OT creada")
    WORK_ORDER_COMPLETED = "work_order.completed", _("OT completada")
    WORK_REQUEST_CREATED = "work_request.created", _("Solicitud creada")


# Evento de "Enviar prueba": no se suscribe, solo se manda a pedido a un destino.
TEST_EVENT = "webhook.test"


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
    """Un evento enviado (o por enviar) a un destino. Los reintentos reutilizan la fila."""

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
    last_error = models.CharField(_("último error"), max_length=300, blank=True)
    last_attempt_at = models.DateTimeField(_("último intento"), null=True, blank=True)
    next_attempt_at = models.DateTimeField(_("próximo intento"), null=True, blank=True)
    delivered_at = models.DateTimeField(_("entregado"), null=True, blank=True)

    class Meta:
        verbose_name = _("entrega de webhook")
        verbose_name_plural = _("entregas de webhook")
        ordering = ["-created_at"]
        indexes = [
            models.Index(
                fields=["organization", "endpoint", "-created_at"], name="webhook_delivery_endpoint"
            ),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "endpoint", "event_id"], name="uniq_webhook_delivery_event"
            )
        ]

    def __str__(self):
        return f"{self.event_type} → {self.endpoint_id}"

    @property
    def event_label(self) -> str:
        if self.event_type == TEST_EVENT:
            return str(_("Prueba"))
        return str(dict(Event.choices).get(self.event_type, self.event_type))
