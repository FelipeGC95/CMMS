"""Avance del asistente de puesta en marcha (ORG-03), uno por organización."""

from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.core.models import TenantModel


class Step(models.TextChoices):
    """Pasos del asistente, en orden (ORG-03 CA1)."""

    INDUSTRY = "industry", _("Rubro")
    LOCATIONS = "locations", _("Sitios")
    ASSETS = "assets", _("Activos")
    PLANS = "plans", _("Planes sugeridos")
    READINGS = "readings", _("Lecturas iniciales")
    CALENDAR = "calendar", _("Calendario")


STEPS = [step.value for step in Step]


class OnboardingState(TenantModel):
    """Qué pasos se completaron o saltaron, en cuál va y qué rubro eligió (ORG-03 CA2).

    Se crea al confirmar el autoregistro (con `started_at` vacío: el inicio lleva
    al asistente) o cuando un administrador lo abre desde Configuración.
    """

    current_step = models.CharField(
        _("paso actual"), max_length=20, choices=Step.choices, default=Step.INDUSTRY
    )
    completed_steps = models.JSONField(_("pasos completados"), default=list, blank=True)
    skipped_steps = models.JSONField(_("pasos saltados"), default=list, blank=True)
    industry = models.CharField(_("rubro"), max_length=30, blank=True)
    started_at = models.DateTimeField(_("abierto por primera vez"), null=True, blank=True)
    finished_at = models.DateTimeField(_("terminado"), null=True, blank=True)
    dismissed_at = models.DateTimeField(_("aviso descartado"), null=True, blank=True)

    class Meta:
        verbose_name = _("puesta en marcha")
        verbose_name_plural = _("puestas en marcha")
        constraints = [
            models.UniqueConstraint(fields=["organization"], name="uniq_onboarding_state_org"),
        ]

    def __str__(self):
        return f"{self.organization_id} · {self.current_step}"

    @property
    def step_number(self) -> int:
        return STEPS.index(self.current_step) + 1

    @property
    def is_finished(self) -> bool:
        return self.finished_at is not None
