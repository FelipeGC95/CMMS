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

    name = models.CharField(_("nombre"), max_length=80)
    # Alcances por recurso (`assets:read`, `inventory:write`, `*:read`…; ver `apps.api.scopes`).
    scopes = models.JSONField(_("alcances"), default=list)
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
    # Limitado por sitio (X-05, ADR-034): ve y toca solo los sitios de `sites` (y lo que cuelga
    # de ellos), igual que una membresía con alcance. Falso = toda la organización.
    restricted_to_sites = models.BooleanField(_("limitado a sitios"), default=False)
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

    def allows(self, *, group: str, write: bool) -> bool:
        from .scopes import allows

        return allows(self.scopes, group=group, write=write)

    @property
    def scope_labels(self) -> list[str]:
        from .scopes import scope_labels

        return scope_labels(self.scopes)


class ApiTokenSite(TenantModel):
    """Sitio al que se limita un token (X-05). El acceso incluye las ubicaciones hijas."""

    token = models.ForeignKey(
        ApiToken, on_delete=models.CASCADE, related_name="sites", verbose_name=_("token")
    )
    location = models.ForeignKey(
        "organizations.Location", on_delete=models.CASCADE, related_name="+", verbose_name=_("sitio")
    )

    class Meta:
        verbose_name = _("sitio del token")
        verbose_name_plural = _("sitios del token")
        constraints = [
            models.UniqueConstraint(fields=["organization", "token", "location"], name="uniq_api_token_site"),
        ]


class ApiIdempotencyKey(TenantModel):
    """`client_uuid` ya usado en una acción de la API sin registro propio (cerrar una OT).

    Reenviar la misma acción con el mismo `client_uuid` devuelve el estado actual
    sin repetirla; usarlo para otra acción u otro objeto es un conflicto.
    """

    client_uuid = models.UUIDField(_("UUID del cliente"))
    action = models.CharField(_("acción"), max_length=40)
    target_id = models.UUIDField(_("objeto"))

    class Meta:
        verbose_name = _("clave de idempotencia")
        verbose_name_plural = _("claves de idempotencia")
        constraints = [
            models.UniqueConstraint(fields=["organization", "client_uuid"], name="uniq_api_idempotency_key")
        ]
