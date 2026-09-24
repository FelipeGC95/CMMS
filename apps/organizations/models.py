from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.core.models import TenantModel, TimeStampedModel


class Organization(TimeStampedModel):
    """Tenant raíz. Todo dato de negocio pertenece a una organización."""

    name = models.CharField(_("nombre"), max_length=150)
    slug = models.SlugField(_("identificador"), unique=True)
    timezone = models.CharField(_("zona horaria"), max_length=64, default="America/Santiago")
    is_active = models.BooleanField(_("activa"), default=True)
    require_close_approval = models.BooleanField(_("requiere aprobación para cerrar OT"), default=False)

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
