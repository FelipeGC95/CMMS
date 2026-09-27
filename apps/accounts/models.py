from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.core.models import TenantModel, TimeStampedModel


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
    role = models.CharField(_("rol"), max_length=20, choices=Role.choices)
    is_active = models.BooleanField(_("activa"), default=True)
    # ORG-06: si está marcado, solo ve lo de sus ubicaciones (MembershipLocation) y descendientes.
    restricted_to_locations = models.BooleanField(_("limitado a ciertas ubicaciones"), default=False)

    class Meta:
        verbose_name = _("membresía")
        verbose_name_plural = _("membresías")
        constraints = [
            models.UniqueConstraint(fields=["organization", "user"], name="uniq_membership"),
        ]

    def __str__(self):
        return f"{self.user} · {self.organization} ({self.get_role_display()})"


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
