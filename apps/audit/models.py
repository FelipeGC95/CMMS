"""Registro de auditoría (X-01). Solo inserción: ver docs/03_modelo_datos.md."""

from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.core.serializers.json import DjangoJSONEncoder
from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.core.models import PlatformQuerySetMixin, TenantManager, TenantModel


class AppendOnlyError(RuntimeError):
    """Se intentó modificar o borrar un evento de auditoría."""


class AuditEventQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise AppendOnlyError("La auditoría es solo de inserción.")

    def delete(self):
        raise AppendOnlyError("La auditoría es solo de inserción.")

    update.queryset_only = True
    delete.queryset_only = True


class PlatformAuditEventQuerySet(PlatformQuerySetMixin, AuditEventQuerySet):
    """`all_tenants` de auditoría: sin RLS (ADR-026) y igual de solo inserción."""

    update = AuditEventQuerySet.update
    delete = AuditEventQuerySet.delete


class AuditEventManager(TenantManager.from_queryset(AuditEventQuerySet)):
    pass


class AuditEvent(TenantModel):
    class ActorType(models.TextChoices):
        USER = "user", _("Usuario")
        SYSTEM = "system", _("Sistema")
        API_TOKEN = "api_token", _("Token de API")

    class Action(models.TextChoices):
        CREATE = "create", _("Creación")
        UPDATE = "update", _("Edición")
        DELETE = "delete", _("Eliminación")
        TRANSITION = "transition", _("Cambio de estado")
        LOGIN = "login", _("Inicio de sesión")
        ADMIN_CREATE = "admin_create", _("Creación en administración")
        ADMIN_UPDATE = "admin_update", _("Edición en administración")
        ADMIN_DELETE = "admin_delete", _("Eliminación en administración")
        # OT: asignación, checklist, costos y término (X-01).
        ASSIGN = "assign", _("Asignación")
        CHECKLIST_EDIT = "checklist_edit", _("Edición de checklist")
        # Respuesta sobrescrita o descartada por llegar con hora anterior (X-06, ADR-011).
        CHECKLIST_ANSWER = "checklist_answer", _("Respuesta de checklist")
        COST_ADD = "cost_add", _("Costo agregado")
        COST_REMOVE = "cost_remove", _("Costo quitado")
        COMPLETE = "complete", _("Término de OT")
        CLOSE = "close", _("Cierre de OT")
        # Equipo y accesos.
        INVITE = "invite", _("Invitación enviada")
        INVITE_REVOKE = "invite_revoke", _("Invitación revocada")
        INVITE_ACCEPT = "invite_accept", _("Invitación aceptada")
        ROLE_CHANGE = "role_change", _("Cambio de rol o alcance")
        DEACTIVATE = "deactivate", _("Desactivación")
        REACTIVATE = "reactivate", _("Reactivación")
        REVOKE = "revoke", _("Revocación")
        # Datos de la organización (X-03, X-04).
        DOWNLOAD = "download", _("Descarga")
        DELETION_REQUEST = "deletion_request", _("Eliminación solicitada")
        DELETION_CANCEL = "deletion_cancel", _("Eliminación cancelada")

    # SET_NULL: el evento sobrevive a la baja del usuario; actor_repr conserva quién fue.
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("actor"),
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    actor_type = models.CharField(_("tipo de actor"), max_length=20, choices=ActorType.choices)
    actor_repr = models.CharField(_("actor"), max_length=254, blank=True)
    action = models.CharField(_("acción"), max_length=20, choices=Action.choices)
    target_type = models.ForeignKey(
        ContentType, verbose_name=_("tipo de objeto"), on_delete=models.PROTECT, related_name="+"
    )
    # Texto y sin FK genérica: el objeto puede borrarse y el evento debe quedar.
    target_id = models.CharField(_("ID del objeto"), max_length=64)
    target_repr = models.CharField(_("objeto"), max_length=200, blank=True)
    changes = models.JSONField(_("cambios"), default=dict, blank=True, encoder=DjangoJSONEncoder)
    ip = models.GenericIPAddressField(_("IP"), null=True, blank=True)
    user_agent = models.CharField(_("navegador"), max_length=300, blank=True)
    request_id = models.CharField(_("ID de petición"), max_length=64, blank=True)

    objects = AuditEventManager()
    all_tenants = models.Manager.from_queryset(PlatformAuditEventQuerySet)()  # noqa: DJ012

    class Meta:
        verbose_name = _("evento de auditoría")
        verbose_name_plural = _("eventos de auditoría")
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["organization", "-created_at"], name="audit_org_created_idx"),
            models.Index(fields=["organization", "target_type", "target_id"], name="audit_org_target_idx"),
        ]

    def __str__(self):
        return f"{self.get_action_display()} · {self.target_repr}"

    @property
    def target_label(self) -> str:
        model = self.target_type.model_class()
        return str(model._meta.verbose_name).capitalize() if model else self.target_type.model

    @property
    def changed_fields(self) -> list[str]:
        """Nombres de los campos del evento (en el admin vienen bajo `fields`)."""
        fields = self.changes.get("fields", self.changes)
        return list(fields) if isinstance(fields, dict) else []

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise AppendOnlyError("La auditoría es solo de inserción.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise AppendOnlyError("La auditoría es solo de inserción.")
