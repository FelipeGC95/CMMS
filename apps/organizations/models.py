from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.core.models import TenantModel, TimeStampedModel


class Plan(TimeStampedModel):
    """Plan comercial: catálogo de plataforma, sin tenant. Cupo en blanco = ilimitado.

    Los cupos y módulos se hacen cumplir en MVP-1 (ORG-10); por ahora solo se registran.
    """

    code = models.SlugField(_("código"), unique=True)
    name = models.CharField(_("nombre"), max_length=100)
    max_assets = models.PositiveIntegerField(_("máximo de activos"), null=True, blank=True)
    max_users = models.PositiveIntegerField(_("máximo de usuarios"), null=True, blank=True)
    max_storage_mb = models.PositiveIntegerField(_("almacenamiento máximo (MB)"), null=True, blank=True)
    features = models.JSONField(_("módulos incluidos"), default=list, blank=True)
    is_active = models.BooleanField(_("activo"), default=True)

    class Meta:
        verbose_name = _("plan")
        verbose_name_plural = _("planes")
        ordering = ["name"]

    def __str__(self):
        return self.name


class Organization(TimeStampedModel):
    """Tenant raíz. Todo dato de negocio pertenece a una organización."""

    class Status(models.TextChoices):
        TRIAL = "trial", _("En prueba")
        ACTIVE = "active", _("Activa")
        SUSPENDED = "suspended", _("Suspendida")
        PENDING_DELETION = "pending_deletion", _("Eliminación solicitada")

    name = models.CharField(_("nombre"), max_length=150)
    slug = models.SlugField(_("identificador"), unique=True)
    plan = models.ForeignKey(
        Plan, on_delete=models.PROTECT, null=True, related_name="organizations", verbose_name=_("plan")
    )
    # Estado comercial; el acceso lo corta `is_active` (el middleware solo mira ese campo).
    status = models.CharField(_("estado"), max_length=20, choices=Status.choices, default=Status.ACTIVE)
    trial_ends_at = models.DateTimeField(_("fin de la prueba"), null=True, blank=True)
    timezone = models.CharField(_("zona horaria"), max_length=64, default="America/Santiago")
    is_active = models.BooleanField(_("activa"), default=True)
    require_close_approval = models.BooleanField(_("requiere aprobación para cerrar OT"), default=False)
    require_failure_details = models.BooleanField(
        _("exige modo y causa de falla para cerrar una OT correctiva"), default=False
    )

    class Meta:
        verbose_name = _("organización")
        verbose_name_plural = _("organizaciones")
        ordering = ["name"]

    def __str__(self):
        return self.name


class Location(TenantModel):
    """Sitio, área o línea, en árbol simple por `parent`."""

    class Kind(models.TextChoices):
        SITE = "site", _("Sitio")
        AREA = "area", _("Área")
        LINE = "line", _("Línea")
        OTHER = "other", _("Otro")

    parent = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="children",
        verbose_name=_("ubicación padre"),
    )
    kind = models.CharField(_("tipo"), max_length=10, choices=Kind.choices, default=Kind.SITE)
    code = models.CharField(_("código"), max_length=30)
    name = models.CharField(_("nombre"), max_length=150)
    is_active = models.BooleanField(_("activa"), default=True)

    class Meta:
        verbose_name = _("ubicación")
        verbose_name_plural = _("ubicaciones")
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(fields=["organization", "code"], name="uniq_location_code"),
        ]

    def __str__(self):
        if self.parent_id:
            return f"{self.parent} › {self.name}"
        return self.name
