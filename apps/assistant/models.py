"""Asistente de diagnóstico con IA (AI-01..05, ADR-018, docs/03 §2.12).

Una conversación pertenece a un activo (y, si aplica, a una OT) y a quien la
inició. Sus mensajes guardan, además del texto visible, los bloques crudos de
la API (`content`) para reenviarlos **sin editar** en el turno siguiente: los
modelos con razonamiento preservado exigen un historial que solo crece
(append-only). Los mensajes no se borran ni se reescriben una vez terminados.
"""

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils.translation import gettext_lazy as _

from apps.core.models import TenantModel


class AssistantConversation(TenantModel):
    class Status(models.TextChoices):
        OPEN = "open", _("Abierta")
        CLOSED = "closed", _("Cerrada")

    class Helpful(models.TextChoices):
        """¿Te sirvió? (AI-04 CA2): forma el conjunto de evaluación del modelo."""

        YES = "yes", _("Sí")
        PARTIAL = "partial", _("En parte")
        NO = "no", _("No")

    class SummaryStatus(models.TextChoices):
        NONE = "", _("Sin nota propuesta")
        PENDING = "pending", _("Nota en preparación")
        DONE = "done", _("Nota propuesta")
        ERROR = "error", _("No se pudo proponer la nota")

    asset = models.ForeignKey(
        "assets.Asset",
        on_delete=models.PROTECT,
        related_name="assistant_conversations",
        verbose_name=_("activo"),
    )
    work_order = models.ForeignKey(
        "work.WorkOrder",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="assistant_conversations",
        verbose_name=_("OT"),
    )
    started_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="+",
        verbose_name=_("iniciada por"),
    )
    title = models.CharField(_("título"), max_length=200)
    status = models.CharField(_("estado"), max_length=10, choices=Status.choices, default=Status.OPEN)
    # Modelo fijo durante toda la conversación: el caché y el razonamiento preservado son por modelo.
    model = models.CharField(_("modelo"), max_length=80)
    helpful = models.CharField(_("¿sirvió?"), max_length=10, choices=Helpful.choices, blank=True)
    helpful_marked_at = models.DateTimeField(_("marcada el"), null=True, blank=True)
    # Nota de cierre propuesta (AI-04 CA1); el técnico la edita o descarta al completar la OT.
    summary = models.TextField(_("nota de cierre propuesta"), blank=True)
    summary_status = models.CharField(
        _("estado de la nota"), max_length=10, choices=SummaryStatus.choices, default="", blank=True
    )
    client_uuid = models.UUIDField(null=True, blank=True, verbose_name=_("UUID del cliente"))

    class Meta:
        verbose_name = _("conversación del asistente")
        verbose_name_plural = _("conversaciones del asistente")
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "client_uuid"], name="uniq_assistant_conversation_client_uuid"
            ),
        ]
        indexes = [
            # Cupo mensual (AI-05 CA1) y lista del administrador.
            models.Index(fields=["organization", "-created_at"], name="asst_conv_org_created_idx"),
            models.Index(fields=["organization", "started_by", "-created_at"], name="asst_conv_org_user_idx"),
            models.Index(fields=["organization", "asset", "-created_at"], name="asst_conv_org_asset_idx"),
            models.Index(
                fields=["organization", "work_order"],
                condition=Q(work_order__isnull=False),
                name="asst_conv_org_wo_idx",
            ),
        ]

    def __str__(self):
        return self.title


class AssistantMessage(TenantModel):
    """Un mensaje de la conversación.

    `kind = turn`: pregunta del usuario o respuesta del asistente, parte del historial.
    `kind = summary`: nota de cierre propuesta; es una llamada aparte que no entra al historial.

    `content` (JSONB) son los mensajes de la API tal como se enviaron o recibieron:
    para el usuario, la lista de bloques de su mensaje (la foto va como referencia
    `galbust_photo` al adjunto y se expande al enviarla); para el asistente, la lista
    de mensajes `assistant`/`user(tool_result)` del turno completo, en orden.
    `sources` (JSONB): en el usuario, las fuentes que el backend entregó en el contexto
    del activo; en el asistente, las fuentes citadas que se validaron contra lo que
    entregaron el contexto y las herramientas en la conversación (las inventadas se
    descartan). `tool_sources`: lo que devolvieron las herramientas en ese turno.
    Un turno con error o rechazado queda con `content` vacío y no se reenvía.
    """

    class Role(models.TextChoices):
        USER = "user", _("Usuario")
        ASSISTANT = "assistant", _("Asistente")

    class Kind(models.TextChoices):
        TURN = "turn", _("Turno")
        SUMMARY = "summary", _("Nota de cierre")

    class Status(models.TextChoices):
        PENDING = "pending", _("Pensando")
        DONE = "done", _("Lista")
        ERROR = "error", _("Error")
        REFUSED = "refused", _("Rechazada")

    conversation = models.ForeignKey(
        AssistantConversation,
        on_delete=models.PROTECT,
        related_name="messages",
        verbose_name=_("conversación"),
    )
    role = models.CharField(_("rol"), max_length=10, choices=Role.choices)
    kind = models.CharField(_("tipo"), max_length=10, choices=Kind.choices, default=Kind.TURN)
    seq = models.PositiveIntegerField(_("orden"))
    text = models.TextField(_("texto"), blank=True)
    content = models.JSONField(_("contenido de la API"), default=list, blank=True)
    sources = models.JSONField(_("fuentes"), default=list, blank=True)
    # Fuentes que devolvieron las herramientas en este turno: lo único que se puede citar después.
    tool_sources = models.JSONField(_("fuentes consultadas"), default=list, blank=True)
    status = models.CharField(_("estado"), max_length=10, choices=Status.choices, default=Status.DONE)
    error_code = models.CharField(_("código de error"), max_length=40, blank=True)
    model = models.CharField(_("modelo"), max_length=80, blank=True)
    input_tokens = models.PositiveIntegerField(_("tokens de entrada"), default=0)
    output_tokens = models.PositiveIntegerField(_("tokens de salida"), default=0)
    cache_read_tokens = models.PositiveIntegerField(_("tokens leídos del caché"), default=0)
    cache_creation_tokens = models.PositiveIntegerField(_("tokens escritos al caché"), default=0)
    tool_calls = models.PositiveSmallIntegerField(_("consultas a herramientas"), default=0)
    completed_at = models.DateTimeField(_("terminado el"), null=True, blank=True)
    client_uuid = models.UUIDField(null=True, blank=True, verbose_name=_("UUID del cliente"))

    class Meta:
        verbose_name = _("mensaje del asistente")
        verbose_name_plural = _("mensajes del asistente")
        ordering = ["conversation", "seq"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "conversation", "seq"], name="uniq_assistant_message_seq"
            ),
            models.UniqueConstraint(
                fields=["organization", "client_uuid"], name="uniq_assistant_message_client_uuid"
            ),
        ]
        indexes = [
            # Costo por organización y mes (AI-05 CA2).
            models.Index(fields=["organization", "created_at"], name="asst_msg_org_created_idx"),
            models.Index(
                fields=["organization", "status"],
                condition=Q(status="pending"),
                name="asst_msg_org_pending_idx",
            ),
        ]

    def __str__(self):
        return f"{self.conversation} · {self.seq}"

    @property
    def is_pending(self) -> bool:
        return self.status == self.Status.PENDING
