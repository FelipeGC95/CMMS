"""Tokens de API por organización (X-05, ADR-005)."""

from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.core.models import TenantModel


class ApiToken(TenantModel):
    """Token de integración de una organización.

    El token completo se muestra una sola vez al crearlo; se guarda solo su
    SHA-256 (es aleatorio y largo: no necesita un hash lento) y un prefijo
    visible para reconocerlo en la lista. Actúa como un principal de servicio
    con su propio usuario técnico (`service_user`, sin clave ni membresía), así
    las OT, lecturas y la auditoría quedan atribuidas al token.
    """

    class Scope(models.TextChoices):
        READ = "read", _("Solo lectura")
        WRITE = "write", _("Lectura y escritura")

    name = models.CharField(_("nombre"), max_length=80)
    scope = models.CharField(_("alcance"), max_length=10, choices=Scope.choices, default=Scope.READ)
    prefix = models.CharField(_("prefijo"), max_length=12, unique=True, editable=False)
    token_hash = models.CharField(_("hash del token"), max_length=64, unique=True, editable=False)
    service_user = models.OneToOneField(
        "accounts.User",
        on_delete=models.PROTECT,
        related_name="api_token",
        editable=False,
        verbose_name=_("usuario de servicio"),
    )
    created_by = models.ForeignKey(
        "accounts.User",
        on_delete=models.SET_NULL,
        null=True,
        related_name="+",
        verbose_name=_("creado por"),
    )
    expires_at = models.DateTimeField(_("vence"), null=True, blank=True)
    last_used_at = models.DateTimeField(_("último uso"), null=True, blank=True)
    revoked_at = models.DateTimeField(_("revocado"), null=True, blank=True)

    class Meta:
        verbose_name = _("token de API")
        verbose_name_plural = _("tokens de API")
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["organization", "-created_at"], name="api_token_org_created")]

    def __str__(self):
        return f"{self.name} ({self.prefix})"

    @property
    def is_expired(self) -> bool:
        return self.expires_at is not None and self.expires_at <= timezone.now()

    @property
    def is_usable(self) -> bool:
        return self.revoked_at is None and not self.is_expired
