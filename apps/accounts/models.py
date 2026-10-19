from decimal import Decimal

from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models.functions import Lower
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.core.models import TenantModel, TimeStampedModel
from apps.core.tenancy import CrossTenantError


class UserManager(BaseUserManager):
    use_in_migrations = True

    def create_user(self, email, password=None, **extra):
        if not email:
            raise ValueError("El correo es obligatorio.")
        user = self.model(email=self.normalize_email(email).lower(), **extra)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, email, password=None, **extra):
        extra.setdefault("is_staff", True)
        extra.setdefault("is_superuser", True)
        return self.create_user(email, password, **extra)


class User(AbstractBaseUser, PermissionsMixin, TimeStampedModel):
    """Usuario global: puede pertenecer a varias organizaciones (Membership)."""

    email = models.EmailField(_("correo"), unique=True)
    full_name = models.CharField(_("nombre completo"), max_length=150, blank=True)
    is_active = models.BooleanField(_("activo"), default=True)
    is_staff = models.BooleanField(_("acceso al panel de administración"), default=False)
    date_joined = models.DateTimeField(default=timezone.now, verbose_name=_("fecha de registro"))
    # Se abre al iniciar sesión (ORG-07); el middleware valida que siga siendo miembro.
    last_organization = models.ForeignKey(
        "organizations.Organization",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
        verbose_name=_("última organización"),
    )

    objects = UserManager()

    USERNAME_FIELD = "email"
    EMAIL_FIELD = "email"
    REQUIRED_FIELDS = []

    class Meta:
        verbose_name = _("usuario")
        verbose_name_plural = _("usuarios")

    def __str__(self):
        return self.full_name or self.email

    @property
    def short_name(self):
        return (self.full_name or self.email).split(" ")[0]


class Membership(TimeStampedModel):
    """Relación usuario ↔ organización con un rol.

    No hereda de TenantModel porque el middleware la consulta antes de conocer
    la organización activa (para listar las organizaciones del usuario).
    """

    class Role(models.TextChoices):
        ADMIN = "admin", _("Administrador")
        PLANNER = "planner", _("Planificador")
        SUPERVISOR = "supervisor", _("Supervisor")
        TECHNICIAN = "technician", _("Técnico")
        VIEWER = "viewer", _("Solo lectura")
        EXTERNAL = "external", _("Externo / contratista")

    user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="memberships", verbose_name=_("usuario")
    )
    organization = models.ForeignKey(
        "organizations.Organization", on_delete=models.CASCADE, related_name="memberships"
    )
    # Con rol personalizado (ORG-12, ADR-031) `role` es su rol base: sigue definiendo el
    # alcance obligatorio de `external`, la vista «Mis OT», la capacidad de técnico y el MFA.
    role = models.CharField(_("rol"), max_length=20, choices=Role.choices)
    custom_role = models.ForeignKey(
        "accounts.CustomRole",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="memberships",
        verbose_name=_("rol personalizado"),
    )
    is_active = models.BooleanField(_("activa"), default=True)
    # ORG-06: si está marcado, solo ve lo de sus ubicaciones (MembershipLocation) y descendientes.
    restricted_to_locations = models.BooleanField(_("limitado a ciertas ubicaciones"), default=False)
    # Capacidad de trabajo (REQ-04/05): horas semanales para el backlog en semanas y la
    # carga del tablero; se reparte en 5 días hábiles. Solo cuenta para técnicos.
    weekly_capacity_hours = models.DecimalField(
        _("horas semanales"),
        max_digits=4,
        decimal_places=1,
        default=Decimal("40"),
        validators=[MinValueValidator(Decimal("0")), MaxValueValidator(Decimal("168"))],
    )

    class Meta:
        verbose_name = _("membresía")
        verbose_name_plural = _("membresías")
        constraints = [
            models.UniqueConstraint(fields=["organization", "user"], name="uniq_membership"),
        ]

    def __str__(self):
        return f"{self.user} · {self.organization} ({self.get_role_display()})"

    def save(self, *args, **kwargs):
        _check_custom_role_tenant(self)
        super().save(*args, **kwargs)

    @property
    def role_label(self):
        """Nombre visible del rol: el personalizado vigente o el rol base (ORG-12)."""
        from .permissions import active_custom_role

        custom = active_custom_role(self)
        return custom.name if custom is not None else self.get_role_display()


def load_custom_role(instance):
    """Rol personalizado de una Membership o Invitation, cargado una vez y guardado en la instancia.

    Ambas se leen antes del contexto de tenant (middleware, enlace de invitación) y ahí RLS
    ocultaría la fila: se busca con la organización explícita (como `_check_related_tenants`).
    """
    if instance.custom_role_id is None:
        return None
    field = instance._meta.get_field("custom_role")
    if not field.is_cached(instance):
        field.set_cached_value(
            instance,
            CustomRole.all_tenants.filter(
                pk=instance.custom_role_id, organization_id=instance.organization_id
            ).first(),
        )
    return field.get_cached_value(instance)


def _check_custom_role_tenant(instance):
    """Membership e Invitation no son TenantModel: su rol personalizado debe ser de su organización."""
    if instance.custom_role_id is None:
        return
    field = instance._meta.get_field("custom_role")
    if field.is_cached(instance):
        role_org_id = instance.custom_role.organization_id
    else:
        # Antes del contexto de tenant (aceptar invitación, middleware): búsqueda puntual
        # con la organización explícita, como `TenantModel._check_related_tenants`.
        role_org_id = (
            CustomRole.all_tenants.filter(pk=instance.custom_role_id)
            .values_list("organization_id", flat=True)
            .first()
        )
    if role_org_id != instance.organization_id:
        raise CrossTenantError(f"{type(instance).__name__}.custom_role apunta a otra organización.")


# Roles base posibles de un rol personalizado: nunca administrador (ORG-12, ADR-031).
CUSTOM_ROLE_BASES = [(value, label) for value, label in Membership.Role.choices if value != "admin"]


class CustomRole(TenantModel):
    """Rol personalizado (ORG-12, ADR-031): un rol base más los permisos elegidos.

    Los permisos son códigos de `accounts.permissions.Perm`, sin los de administración
    (`ADMIN_ONLY_PERMS`); los valida el servicio. No se borra: se desactiva.
    """

    name = models.CharField(_("nombre"), max_length=60)
    base_role = models.CharField(_("rol base"), max_length=20, choices=CUSTOM_ROLE_BASES)
    permissions = models.JSONField(_("permisos"), default=list, blank=True)
    is_active = models.BooleanField(_("activo"), default=True)

    class Meta:
        verbose_name = _("rol personalizado")
        verbose_name_plural = _("roles personalizados")
        constraints = [
            models.UniqueConstraint("organization", Lower("name"), name="uniq_custom_role_name"),
        ]

    def __str__(self):
        return self.name

    @property
    def permission_set(self) -> frozenset:
        from .permissions import CUSTOMIZABLE_PERMS, Perm

        return frozenset(Perm(code) for code in self.permissions if code in CUSTOMIZABLE_PERMS)


class MembershipLocation(TenantModel):
    """Alcance por sitio de una membresía (ORG-06). El acceso incluye las ubicaciones hijas."""

    membership = models.ForeignKey(
        Membership, on_delete=models.CASCADE, related_name="scope", verbose_name=_("membresía")
    )
    location = models.ForeignKey(
        "organizations.Location", on_delete=models.CASCADE, related_name="+", verbose_name=_("ubicación")
    )

    class Meta:
        verbose_name = _("ubicación permitida")
        verbose_name_plural = _("ubicaciones permitidas")
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "membership", "location"], name="uniq_membership_location"
            ),
        ]


class Invitation(TimeStampedModel):
    """Invitación por correo (ORG-05).

    Como Membership, no hereda de TenantModel: se busca por token antes de que
    el invitado pertenezca a la organización. Las vistas del equipo la filtran
    siempre por la organización activa.
    """

    organization = models.ForeignKey(
        "organizations.Organization", on_delete=models.CASCADE, related_name="invitations"
    )
    email = models.EmailField(_("correo"))
    role = models.CharField(_("rol"), max_length=20, choices=Membership.Role.choices)
    # ORG-12: si se invita con un rol personalizado, `role` es su rol base.
    custom_role = models.ForeignKey(
        CustomRole,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
        verbose_name=_("rol personalizado"),
    )
    token_hash = models.CharField(
        max_length=64, unique=True, editable=False, verbose_name=_("hash del token")
    )
    invited_by = models.ForeignKey(
        User, null=True, on_delete=models.SET_NULL, related_name="+", verbose_name=_("invitado por")
    )
    expires_at = models.DateTimeField(_("vence"))
    accepted_at = models.DateTimeField(_("aceptada"), null=True, blank=True)
    revoked_at = models.DateTimeField(_("revocada"), null=True, blank=True)

    class Meta:
        verbose_name = _("invitación")
        verbose_name_plural = _("invitaciones")
        indexes = [models.Index(fields=["organization", "email"], name="invitation_org_email")]

    def __str__(self):
        return f"{self.email} · {self.organization}"

    def save(self, *args, **kwargs):
        _check_custom_role_tenant(self)
        super().save(*args, **kwargs)

    @property
    def role_label(self):
        custom = load_custom_role(self)
        return custom.name if custom is not None and custom.is_active else self.get_role_display()

    @property
    def is_pending(self):
        return self.accepted_at is None and self.revoked_at is None and self.expires_at > timezone.now()


class PendingSignup(TimeStampedModel):
    """Autoregistro esperando la verificación del correo (ORG-02 CA2).

    No se crea usuario, organización ni membresía hasta que se abre el enlace del
    correo: ahí se crean los tres en una transacción. Es de plataforma (sin tenant):
    todavía no existe la organización. La contraseña se guarda ya con hash.
    """

    email = models.EmailField(_("correo"))
    full_name = models.CharField(_("nombre completo"), max_length=150)
    organization_name = models.CharField(_("nombre de la organización"), max_length=150)
    password_hash = models.CharField(_("hash de la contraseña"), max_length=256, editable=False)
    token_hash = models.CharField(_("hash del token"), max_length=64, unique=True, editable=False)
    expires_at = models.DateTimeField(_("vence"))
    completed_at = models.DateTimeField(_("completado"), null=True, blank=True)

    class Meta:
        verbose_name = _("registro pendiente")
        verbose_name_plural = _("registros pendientes")
        indexes = [models.Index(fields=["email"], name="pending_signup_email")]

    def __str__(self):
        return self.email
