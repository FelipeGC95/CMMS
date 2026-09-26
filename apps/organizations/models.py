import uuid

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


def organization_logo_upload_to(instance, filename):
    """Mismo prefijo por organización que los adjuntos (ADR-014); nombre nuevo en cada cambio."""
    return f"org/{instance.pk}/logo-{uuid.uuid4().hex}.png"


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
    # Nombres visibles propios de los estados de OT (WO-02 CA5): {"in_progress": "En faena"}.
    # Solo claves de `WorkOrder.Status`; una clave ausente usa el nombre traducido por defecto.
    work_order_status_labels = models.JSONField(_("nombres de los estados de OT"), default=dict, blank=True)
    # Logo para etiquetas QR y reportes (ORG-09, parte mínima). PNG normalizado por el servicio.
    logo = models.ImageField(_("logo"), upload_to=organization_logo_upload_to, blank=True)

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
    # En blanco hereda la de la organización (ORG-04 CA1).
    timezone = models.CharField(_("zona horaria"), max_length=64, blank=True)
    is_active = models.BooleanField(_("activa"), default=True)
    # Nombre con sus ancestros ("Planta › Área › Línea"), guardado para mostrar la
    # ubicación sin recorrer `parent` fila por fila (evita N+1 en listas y selects).
    path = models.CharField(_("ruta"), max_length=1000, blank=True, editable=False)

    PATH_SEPARATOR = " › "

    class Meta:
        verbose_name = _("ubicación")
        verbose_name_plural = _("ubicaciones")
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(fields=["organization", "code"], name="uniq_location_code"),
        ]

    def __str__(self):
        return self.path or self.name

    def save(self, *args, **kwargs):
        previous_path = None
        if not self._state.adding:
            previous_path = Location.objects.filter(pk=self.pk).values_list("path", flat=True).first()
        parent_path = (self.parent.path or self.parent.name) if self.parent_id else ""
        self.path = f"{parent_path}{self.PATH_SEPARATOR}{self.name}" if parent_path else self.name
        self.path = self.path[: self._meta.get_field("path").max_length]
        update_fields = kwargs.get("update_fields")
        if update_fields is not None and "path" not in update_fields:
            kwargs["update_fields"] = [*update_fields, "path"]
        super().save(*args, **kwargs)
        if previous_path is not None and previous_path != self.path:
            self._refresh_descendant_paths()

    def _refresh_descendant_paths(self):
        """Al renombrar o mover, las rutas del subárbol se recalculan en una lectura y una escritura."""
        rows = list(Location.objects.all())
        children: dict = {}
        for row in rows:
            children.setdefault(row.parent_id, []).append(row)
        changed, pending = [], [(self.pk, self.path)]
        seen = {self.pk}
        max_length = self._meta.get_field("path").max_length
        while pending:
            parent_id, parent_path = pending.pop()
            for child in children.get(parent_id, []):
                if child.pk in seen:
                    continue
                seen.add(child.pk)
                new_path = f"{parent_path}{self.PATH_SEPARATOR}{child.name}"[:max_length]
                if child.path != new_path:
                    child.path = new_path
                    changed.append(child)
                pending.append((child.pk, new_path))
        Location.objects.bulk_update(changed, ["path"])
