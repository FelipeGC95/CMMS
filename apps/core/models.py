from django.db import models
from uuid_utils import compat

from .tenancy import CrossTenantError, require_current_organization


def uuid7():
    """UUID versión 7 (ordenable por tiempo). Ver ADR-009."""
    return compat.uuid7()


class TimeStampedModel(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class TenantManager(models.Manager):
    """Filtra por la organización del contexto; sin contexto lanza TenantNotSet."""

    def get_queryset(self):
        organization = require_current_organization()
        return super().get_queryset().filter(organization=organization)


class TenantModel(TimeStampedModel):
    """Base de todo dato de negocio. Ver reglas en CLAUDE.md."""

    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        editable=False,
        related_name="+",
    )

    objects = TenantManager()
    # Sin filtro: solo para código de plataforma revisado (comandos, migraciones).
    all_tenants = models.Manager()  # noqa: DJ012

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        organization = require_current_organization()
        if self.organization_id is None:
            self.organization = organization
        elif self.organization_id != organization.id:
            raise CrossTenantError(f"{type(self).__name__} pertenece a otra organización.")
        self._check_related_tenants()
        super().save(*args, **kwargs)

    def _check_related_tenants(self):
        """Impide apuntar a filas de otra organización mediante una FK."""
        for field in self._meta.concrete_fields:
            if not field.is_relation or field.name == "organization":
                continue
            related_model = field.related_model
            if not issubclass(related_model, TenantModel):
                continue
            value = getattr(self, field.attname)
            if value is None:
                continue
            if field.is_cached(self):
                related_org_id = getattr(self, field.name).organization_id
            else:
                related_org_id = (
                    related_model.all_tenants.filter(pk=value)
                    .values_list("organization_id", flat=True)
                    .first()
                )
            if related_org_id != self.organization_id:
                raise CrossTenantError(f"{type(self).__name__}.{field.name} apunta a otra organización.")
