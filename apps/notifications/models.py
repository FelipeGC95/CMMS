"""Notificaciones en la app y preferencias por tipo y canal (X-02)."""

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils.translation import gettext_lazy as _

from apps.core.models import TenantModel


class Kind(models.TextChoices):
    WO_ASSIGNED = "wo_assigned", _("OT asignada a mí")
    WO_SCHEDULED = "wo_scheduled", _("OT programada para mí")
    WO_DUE_SOON = "wo_due_soon", _("OT por vencer")
    WO_OVERDUE = "wo_overdue", _("OT vencida")
    REQUEST_NEW = "request_new", _("Nueva solicitud")
    REQUEST_RESOLVED = "request_resolved", _("Mi solicitud resuelta")


class Notification(TenantModel):
    """Un aviso para una persona. Se guarda aunque solo vaya por correo (`in_app=False`):
    así el envío diario de vencimientos no se repite (`dedupe_key`)."""

    recipient = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="+",
        verbose_name=_("destinatario"),
    )
    kind = models.CharField(_("tipo"), max_length=30, choices=Kind.choices)
    title = models.CharField(_("título"), max_length=200)
    body = models.CharField(_("detalle"), max_length=500, blank=True)
    url = models.CharField(_("enlace"), max_length=300, blank=True)
    work_order = models.ForeignKey(
        "work.WorkOrder",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("OT"),
    )
    work_request = models.ForeignKey(
        "work.WorkRequest",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("solicitud"),
    )
    in_app = models.BooleanField(_("en la app"), default=True)
    dedupe_key = models.CharField(_("clave de envío único"), max_length=100, blank=True)
    read_at = models.DateTimeField(_("leída"), null=True, blank=True)

    class Meta:
        verbose_name = _("notificación")
        verbose_name_plural = _("notificaciones")
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["organization", "recipient", "read_at"], name="notif_org_unread_idx"),
            models.Index(fields=["organization", "recipient", "-created_at"], name="notif_org_recent_idx"),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "recipient", "dedupe_key"],
                condition=~Q(dedupe_key=""),
                name="uniq_notification_dedupe",
            ),
        ]

    def __str__(self):
        return self.title


class NotificationPreference(TenantModel):
    """Canales elegidos por la persona para un tipo de aviso. Sin fila: ambos activos."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+", verbose_name=_("usuario")
    )
    kind = models.CharField(_("tipo"), max_length=30, choices=Kind.choices)
    in_app = models.BooleanField(_("en la app"), default=True)
    email = models.BooleanField(_("por correo"), default=True)

    class Meta:
        verbose_name = _("preferencia de notificación")
        verbose_name_plural = _("preferencias de notificación")
        constraints = [
            models.UniqueConstraint(fields=["organization", "user", "kind"], name="uniq_notification_pref"),
        ]

    def __str__(self):
        return f"{self.user} · {self.get_kind_display()}"
