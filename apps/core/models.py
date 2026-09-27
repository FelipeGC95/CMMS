from django.db import models
from uuid_utils import compat

from .tenancy import CrossTenantError, platform_context, require_current_organization


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


class PlatformQuerySetMixin:
    """Evalúa la consulta con RLS desactivado (`platform_context`, ADR-026).

    Es el queryset de `all_tenants`: sin filtro por organización a propósito,
    así que tampoco debe filtrarlo la política de la base. Cubre las formas de
    evaluar un queryset; usado como subconsulta dentro de otra consulta, rige
    el contexto de la consulta externa.
    """

    def _fetch_all(self):
        with platform_context():
            super()._fetch_all()

    def iterator(self, chunk_size=None):
        with platform_context():
            yield from super().iterator(chunk_size)

    def count(self):
        with platform_context():
            return super().count()

    def exists(self):
        with platform_context():
            return super().exists()

    def contains(self, obj):
        with platform_context():
            return super().contains(obj)

    def aggregate(self, *args, **kwargs):
        with platform_context():
            return super().aggregate(*args, **kwargs)

    def update(self, **kwargs):
        with platform_context():
            return super().update(**kwargs)

    def _update(self, values):
        with platform_context():
            return super()._update(values)

    def delete(self):
        with platform_context():
            return super().delete()

    def _raw_delete(self, using):
        with platform_context():
            return super()._raw_delete(using)

    def bulk_create(self, *args, **kwargs):
        with platform_context():
            return super().bulk_create(*args, **kwargs)

    def bulk_update(self, *args, **kwargs):
        with platform_context():
            return super().bulk_update(*args, **kwargs)

    def explain(self, *args, **kwargs):
        with platform_context():
            return super().explain(*args, **kwargs)

    # Mismos atributos que QuerySet (los pierde al redefinir los métodos).
    update.alters_data = True
    _update.alters_data = True
    _update.queryset_only = False
    delete.alters_data = True
    delete.queryset_only = True
    _raw_delete.alters_data = True
    bulk_create.alters_data = True
    bulk_update.alters_data = True


class PlatformQuerySet(PlatformQuerySetMixin, models.QuerySet):
    pass


PlatformManager = models.Manager.from_queryset(PlatformQuerySet)


class TenantModel(TimeStampedModel):
    """Base de todo dato de negocio. Ver reglas en CLAUDE.md."""

    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        editable=False,
        related_name="+",
    )

    objects = TenantManager()
    # Sin filtro y con RLS desactivado (ADR-026): solo para código de plataforma
    # revisado (comandos, migraciones) o búsquedas previas al contexto.
    all_tenants = PlatformManager()  # noqa: DJ012

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
