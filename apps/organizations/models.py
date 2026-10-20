import re
import uuid
from decimal import Decimal

from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.core.models import TenantModel, TimeStampedModel


class Plan(TimeStampedModel):
    """Plan comercial: catálogo de plataforma, sin tenant. Cupo en blanco = ilimitado.

    Cupo de activos y módulos se hacen cumplir (ORG-10): `organizations.services.check_quota`
    y `organizations.modules`. `features` vacío = sin restricción de módulos.
    """

    code = models.SlugField(_("código"), unique=True)
    name = models.CharField(_("nombre"), max_length=100)
    max_assets = models.PositiveIntegerField(_("máximo de activos"), null=True, blank=True)
    max_users = models.PositiveIntegerField(_("máximo de usuarios"), null=True, blank=True)
    max_storage_mb = models.PositiveIntegerField(_("almacenamiento máximo (MB)"), null=True, blank=True)
    # Conversaciones nuevas del asistente por mes calendario (AI-05 CA1); en blanco = sin límite.
    max_assistant_conversations = models.PositiveIntegerField(
        _("conversaciones del asistente por mes"), null=True, blank=True
    )
    features = models.JSONField(_("módulos incluidos"), default=list, blank=True)
    is_active = models.BooleanField(_("activo"), default=True)

    # Precio (ORG-13, ADR-033), en UF por mes; lo edita el superadmin. En blanco = no se vende en
    # línea (Prueba) o se cotiza por organización (Empresa: `Organization.custom_monthly_uf`).
    monthly_price_uf = models.DecimalField(
        _("precio mensual (UF)"), max_digits=7, decimal_places=2, null=True, blank=True
    )
    # Tramos por activos: sobre `price_included_assets` se suma `price_block_uf` por cada bloque de
    # `price_block_assets` activos (o fracción). En blanco = sin tramos.
    price_included_assets = models.PositiveIntegerField(
        _("activos incluidos en el precio"), null=True, blank=True
    )
    price_block_assets = models.PositiveIntegerField(_("activos por bloque adicional"), default=100)
    price_block_uf = models.DecimalField(
        _("precio por bloque adicional (UF)"), max_digits=7, decimal_places=2, default=Decimal("0")
    )
    # Anual = 10 meses (2 gratis).
    annual_months_charged = models.PositiveSmallIntegerField(
        _("meses cobrados en el pago anual"),
        default=10,
        validators=[MinValueValidator(1), MaxValueValidator(12)],
    )
    is_self_service = models.BooleanField(
        _("contratable en línea"),
        default=False,
        help_text=_("Aparece en Facturación con checkout. Empresa se cotiza: déjalo apagado."),
    )

    class Meta:
        verbose_name = _("plan")
        verbose_name_plural = _("planes")
        ordering = ["name"]

    def __str__(self):
        return self.name


class Coupon(TimeStampedModel):
    """Descuento comercial (ORG-13): catálogo de plataforma que el superadmin aplica a una organización.

    Rige `duration_months` meses desde que se aplica (en blanco = mientras siga aplicado).
    """

    code = models.SlugField(_("código"), unique=True)
    name = models.CharField(_("nombre"), max_length=100)
    percent_off = models.PositiveSmallIntegerField(
        _("descuento (%)"), validators=[MinValueValidator(1), MaxValueValidator(100)]
    )
    duration_months = models.PositiveSmallIntegerField(_("duración (meses)"), null=True, blank=True)
    is_active = models.BooleanField(_("activo"), default=True)

    class Meta:
        verbose_name = _("cupón")
        verbose_name_plural = _("cupones")
        ordering = ["name"]

    def __str__(self):
        return f"{self.name} ({self.percent_off} %)"


# Sufijo societario final ("SpA", "S.A.", "Ltda.", "EIRL"...), que se omite en la barra superior.
LEGAL_SUFFIX = re.compile(
    r"[\s,]+(s\.?\s?p\.?\s?a\.?|s\.?\s?a\.?|ltda\.?|limitada|e\.?\s?i\.?\s?r\.?\s?l\.?)$", re.I
)


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
        # Lápida tras la purga (X-04): sin datos, solo id, fechas y resumen.
        DELETED = "deleted", _("Eliminada")

    class MfaPolicy(models.TextChoices):
        """Quién debe usar verificación en dos pasos (ORG-08). Por defecto, los administradores."""

        ADMINS_ONLY = "admins_only", _("Obligatoria para administradores")
        EVERYONE = "everyone", _("Obligatoria para todos")
        OPTIONAL = "optional", _("Opcional para todos")

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
    # Aviso por QR sin cuenta (REQ-02): apagado por defecto; lo enciende el administrador.
    allow_anonymous_reports = models.BooleanField(_("permite avisos por QR sin cuenta"), default=False)
    # Nombres visibles propios de los estados de OT (WO-02 CA5): {"in_progress": "En faena"}.
    # Solo claves de `WorkOrder.Status`; una clave ausente usa el nombre traducido por defecto.
    work_order_status_labels = models.JSONField(_("nombres de los estados de OT"), default=dict, blank=True)
    # Logo para etiquetas QR y reportes (ORG-09, parte mínima). PNG normalizado por el servicio.
    logo = models.ImageField(_("logo"), upload_to=organization_logo_upload_to, blank=True)
    # Tamaño del logo guardado: cuenta para el cupo de almacenamiento (ORG-10).
    logo_size_bytes = models.PositiveIntegerField(_("tamaño del logo (bytes)"), default=0)
    # Color primario de la marca (ORG-09), "#RRGGBB"; en blanco usa el de GALBUST.
    # Los tonos por uso con contraste AA se calculan en `branding.brand_palette`.
    primary_color = models.CharField(_("color primario"), max_length=7, blank=True)
    # Verificación en dos pasos exigida al entrar (ORG-08); se aplica la de la organización activa.
    mfa_policy = models.CharField(
        _("verificación en dos pasos"),
        max_length=20,
        choices=MfaPolicy.choices,
        default=MfaPolicy.ADMINS_ONLY,
    )

    # Cobro (ORG-13, ADR-033). Exenta = facturación manual: nunca pasa a solo lectura por impago.
    # Por defecto exenta (las creadas por el superadmin y la demo); el autoregistro la crea sin exención.
    billing_exempt = models.BooleanField(
        _("facturación manual (exenta)"),
        default=True,
        help_text=_("Sin cobro en línea: no se bloquea por fin de prueba ni por impago."),
    )
    # Acceso pagado hasta (fin del período pagado + tolerancia); lo mueve cada pago aprobado.
    paid_through = models.DateTimeField(_("pagada hasta"), null=True, blank=True)
    # Primer cobro rechazado sin pago posterior: desde aquí corre la gracia (se borra al pagar).
    past_due_since = models.DateTimeField(_("pago pendiente desde"), null=True, blank=True)
    # Precio cotizado (Empresa u otro acuerdo), en UF por mes; reemplaza al del plan.
    custom_monthly_uf = models.DecimalField(
        _("precio acordado (UF/mes)"), max_digits=7, decimal_places=2, null=True, blank=True
    )
    coupon = models.ForeignKey(
        Coupon,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="organizations",
        verbose_name=_("cupón"),
    )
    coupon_applied_at = models.DateTimeField(_("cupón aplicado el"), null=True, blank=True)

    # Borrado a pedido (X-04): período de gracia y lápida tras la purga (docs/07_operacion.md).
    deletion_requested_at = models.DateTimeField(_("eliminación solicitada el"), null=True, blank=True)
    deletion_scheduled_for = models.DateTimeField(_("se eliminará el"), null=True, blank=True)
    deletion_requested_by = models.ForeignKey(
        "accounts.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("eliminación solicitada por"),
    )
    deleted_at = models.DateTimeField(_("eliminada el"), null=True, blank=True)
    purge_summary = models.JSONField(_("resumen de la purga"), default=dict, blank=True)

    class Meta:
        verbose_name = _("organización")
        verbose_name_plural = _("organizaciones")
        ordering = ["name"]

    def __str__(self):
        return self.name

    @property
    def short_name(self):
        """Nombre sin el sufijo societario, para el selector compacto de la barra superior."""
        return LEGAL_SUFFIX.sub("", self.name.strip()) or self.name


def data_export_upload_to(instance, filename):
    """Bajo el prefijo de la organización (ADR-014): se sirve solo por la vista con permisos."""
    return f"org/{instance.organization_id}/exports/{uuid.uuid4().hex}.zip"


class DataExport(TenantModel):
    """Exportación completa de los datos de la organización (X-03): un ZIP que vence."""

    class Status(models.TextChoices):
        PENDING = "pending", _("En preparación")
        READY = "ready", _("Lista")
        FAILED = "failed", _("Falló")
        EXPIRED = "expired", _("Vencida")

    requested_by = models.ForeignKey(
        "accounts.User",
        on_delete=models.SET_NULL,
        null=True,
        related_name="+",
        verbose_name=_("solicitada por"),
    )
    status = models.CharField(_("estado"), max_length=10, choices=Status.choices, default=Status.PENDING)
    file = models.FileField(_("archivo"), upload_to=data_export_upload_to, blank=True)
    size_bytes = models.PositiveBigIntegerField(_("tamaño (bytes)"), default=0)
    finished_at = models.DateTimeField(_("terminada el"), null=True, blank=True)
    expires_at = models.DateTimeField(_("vence"), null=True, blank=True)
    error = models.CharField(_("error"), max_length=300, blank=True)

    class Meta:
        verbose_name = _("exportación de datos")
        verbose_name_plural = _("exportaciones de datos")
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["organization", "-created_at"], name="data_export_org_created")]

    def __str__(self):
        return f"{self._meta.verbose_name} {self.created_at:%Y-%m-%d %H:%M}" if self.created_at else ""


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
