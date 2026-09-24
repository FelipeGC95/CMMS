from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.core.models import TimeStampedModel


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
    date_joined = models.DateTimeField(default=timezone.now)

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

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="memberships")
    organization = models.ForeignKey(
        "organizations.Organization", on_delete=models.CASCADE, related_name="memberships"
    )
    role = models.CharField(_("rol"), max_length=20, choices=Role.choices)
    is_active = models.BooleanField(_("activa"), default=True)

    class Meta:
        verbose_name = _("membresía")
        verbose_name_plural = _("membresías")
        constraints = [
            models.UniqueConstraint(fields=["organization", "user"], name="uniq_membership"),
        ]

    def __str__(self):
        return f"{self.user} · {self.organization} ({self.get_role_display()})"
