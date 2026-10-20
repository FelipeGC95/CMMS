"""Inicio de sesión único por OIDC (ORG-11, ADR-032): configuración y dominios por organización."""

from django.db import models
from django.db.models import Q
from django.utils.translation import gettext_lazy as _

from apps.accounts.models import CUSTOM_ROLE_BASES
from apps.core.models import TenantModel

# Registro TXT que prueba que la organización controla el dominio (ADR-032).
TXT_RECORD_PREFIX = "_galbust-verification"
TXT_VALUE_PREFIX = "galbust-domain-verification="


class SsoConfiguration(TenantModel):
    """Proveedor de identidad OIDC de la organización (una por organización).

    El secreto del cliente se guarda cifrado (`apps.sso.crypto`) y nunca se vuelve a mostrar.
    `provider_metadata` guarda lo necesario del documento de descubrimiento (emisor y
    endpoints), validado al guardar o probar la conexión.
    """

    discovery_url = models.URLField(_("URL de descubrimiento"), max_length=500)
    client_id = models.CharField(_("ID de cliente"), max_length=255)
    client_secret_encrypted = models.TextField(_("secreto del cliente (cifrado)"), blank=True, editable=False)
    # Rol de quien entra por primera vez por SSO: nunca administrador (ORG-11 CA6).
    default_role = models.CharField(
        _("rol por defecto"), max_length=20, choices=CUSTOM_ROLE_BASES, default="technician"
    )
    default_custom_role = models.ForeignKey(
        "accounts.CustomRole",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
        verbose_name=_("rol personalizado por defecto"),
    )
    enforce_sso = models.BooleanField(_("exigir SSO"), default=False)
    is_active = models.BooleanField(_("activo"), default=False)
    provider_metadata = models.JSONField(_("datos del proveedor"), default=dict, blank=True)
    metadata_fetched_at = models.DateTimeField(_("datos del proveedor leídos"), null=True, blank=True)
    last_tested_at = models.DateTimeField(_("última prueba"), null=True, blank=True)
    last_test_ok = models.BooleanField(_("última prueba correcta"), null=True, blank=True)
    last_test_error = models.CharField(_("error de la última prueba"), max_length=300, blank=True)

    class Meta:
        verbose_name = _("configuración SSO")
        verbose_name_plural = _("configuraciones SSO")
        constraints = [
            models.UniqueConstraint(fields=["organization"], name="uniq_sso_configuration"),
        ]

    def __str__(self):
        return self.discovery_url

    @property
    def has_secret(self) -> bool:
        return bool(self.client_secret_encrypted)


class SsoDomain(TenantModel):
    """Dominio de correo de la organización. Solo un dominio verificado activa el SSO.

    Varias organizaciones pueden reclamarlo mientras está pendiente (así nadie lo bloquea
    sin ser dueño); al verificarlo pasa a ser de una sola en toda la plataforma (índice
    único parcial sobre los verificados).
    """

    domain = models.CharField(_("dominio"), max_length=253)
    verification_token = models.CharField(_("código de verificación"), max_length=64, editable=False)
    verified_at = models.DateTimeField(_("verificado"), null=True, blank=True)
    last_checked_at = models.DateTimeField(_("última comprobación"), null=True, blank=True)

    class Meta:
        verbose_name = _("dominio SSO")
        verbose_name_plural = _("dominios SSO")
        ordering = ["domain"]
        constraints = [
            models.UniqueConstraint(fields=["organization", "domain"], name="uniq_sso_domain_org"),
            models.UniqueConstraint(
                fields=["domain"], condition=Q(verified_at__isnull=False), name="uniq_sso_domain_verified"
            ),
        ]

    def __str__(self):
        return self.domain

    @property
    def is_verified(self) -> bool:
        return self.verified_at is not None

    @property
    def txt_name(self) -> str:
        return f"{TXT_RECORD_PREFIX}.{self.domain}"

    @property
    def txt_value(self) -> str:
        return f"{TXT_VALUE_PREFIX}{self.verification_token}"
