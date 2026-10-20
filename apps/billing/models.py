"""Cobro recurrente y autogestión del plan (ORG-13, ADR-033).

`BillingAccount` y `Payment` son datos de la organización (TenantModel, con RLS). `UfValue` y
`ProviderEvent` son de plataforma: el valor diario de la UF es uno solo para todas, y los eventos
del webhook llegan antes de saber a qué organización pertenecen.
"""

import secrets

from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.core.models import TenantModel, TimeStampedModel


def new_external_reference() -> str:
    """Referencia opaca que viaja a Mercado Pago y vuelve en la suscripción (no revela la organización)."""
    return f"gb_{secrets.token_urlsafe(24)}"


class Period(models.TextChoices):
    MONTHLY = "monthly", _("Mensual")
    ANNUAL = "annual", _("Anual")


PERIOD_MONTHS = {Period.MONTHLY: 1, Period.ANNUAL: 12}


class UfValue(TimeStampedModel):
    """Valor de la UF de un día (fuente pública). Si la fuente falla se usa el último guardado."""

    date = models.DateField(_("fecha"), unique=True)
    value = models.DecimalField(_("valor (CLP)"), max_digits=10, decimal_places=2)
    source = models.CharField(_("fuente"), max_length=100, blank=True)

    class Meta:
        verbose_name = _("valor de la UF")
        verbose_name_plural = _("valores de la UF")
        ordering = ["-date"]

    def __str__(self):
        return f"{self.date:%d-%m-%Y}: {self.value}"


class BillingAccount(TenantModel):
    """Suscripción y datos de facturación de una organización (una por organización)."""

    class Status(models.TextChoices):
        NONE = "none", _("Sin suscripción")
        PENDING = "pending", _("Pago por confirmar")
        AUTHORIZED = "authorized", _("Activa")
        PAUSED = "paused", _("Pausada")
        CANCELLED = "cancelled", _("Cancelada")

    plan = models.ForeignKey(
        "organizations.Plan",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("plan contratado"),
    )
    period = models.CharField(_("período"), max_length=10, choices=Period.choices, default=Period.MONTHLY)
    status = models.CharField(_("estado"), max_length=12, choices=Status.choices, default=Status.NONE)
    provider = models.CharField(_("proveedor"), max_length=20, default="mercadopago")
    provider_subscription_id = models.CharField(
        _("id de la suscripción en el proveedor"), max_length=64, blank=True, db_index=True
    )
    external_reference = models.CharField(
        _("referencia externa"), max_length=64, unique=True, default=new_external_reference, editable=False
    )
    checkout_url = models.URLField(_("enlace de pago"), max_length=500, blank=True)
    # Monto que hoy cobra el proveedor por período, y con qué UF se calculó.
    amount_clp = models.PositiveIntegerField(_("monto por período (CLP)"), null=True, blank=True)
    amount_uf = models.DecimalField(
        _("monto por período (UF)"), max_digits=9, decimal_places=4, null=True, blank=True
    )
    uf_value = models.DecimalField(_("UF aplicada"), max_digits=10, decimal_places=2, null=True, blank=True)
    amount_updated_at = models.DateTimeField(_("monto actualizado el"), null=True, blank=True)
    next_charge_at = models.DateTimeField(_("próximo cobro"), null=True, blank=True)
    # Baja de plan programada para el fin del período (ORG-13 CA4).
    pending_plan = models.ForeignKey(
        "organizations.Plan",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("cambio de plan programado"),
    )
    # Datos de facturación (ORG-13 CA7).
    tax_id = models.CharField(_("RUT"), max_length=12, blank=True)
    legal_name = models.CharField(_("razón social"), max_length=150, blank=True)
    business_activity = models.CharField(_("giro"), max_length=150, blank=True)
    billing_address = models.CharField(_("dirección"), max_length=250, blank=True)
    billing_email = models.EmailField(_("correo de facturación"), blank=True)
    # Avisos ya enviados (clave → fecha ISO), para no repetirlos (X-02).
    notices = models.JSONField(_("avisos enviados"), default=dict, blank=True)

    class Meta:
        verbose_name = _("cuenta de facturación")
        verbose_name_plural = _("cuentas de facturación")
        constraints = [models.UniqueConstraint(fields=["organization"], name="uniq_billing_account_org")]

    def __str__(self):
        return str(_("Facturación"))

    @property
    def is_active_subscription(self) -> bool:
        return self.status == self.Status.AUTHORIZED


class Payment(TenantModel):
    """Un cobro informado por el proveedor (lo consultamos a su API, no confiamos en el webhook)."""

    class Status(models.TextChoices):
        APPROVED = "approved", _("Aprobado")
        PENDING = "pending", _("Pendiente")
        REJECTED = "rejected", _("Rechazado")
        REFUNDED = "refunded", _("Devuelto")
        CANCELLED = "cancelled", _("Anulado")

    account = models.ForeignKey(
        BillingAccount, on_delete=models.PROTECT, related_name="payments", verbose_name=_("cuenta")
    )
    # Id del pago en el proveedor: único en toda la plataforma (un pago nunca es de dos organizaciones).
    provider_payment_id = models.CharField(_("id del pago en el proveedor"), max_length=64)
    status = models.CharField(_("estado"), max_length=10, choices=Status.choices)
    status_detail = models.CharField(_("detalle"), max_length=100, blank=True)
    amount_clp = models.PositiveIntegerField(_("monto (CLP)"))
    amount_uf = models.DecimalField(_("monto (UF)"), max_digits=9, decimal_places=4, null=True, blank=True)
    uf_value = models.DecimalField(_("UF aplicada"), max_digits=10, decimal_places=2, null=True, blank=True)
    plan = models.ForeignKey(
        "organizations.Plan",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("plan"),
    )
    period = models.CharField(_("período"), max_length=10, choices=Period.choices, blank=True)
    paid_at = models.DateTimeField(_("fecha de pago"), null=True, blank=True)
    period_end = models.DateTimeField(_("cubre hasta"), null=True, blank=True)

    class Meta:
        verbose_name = _("pago")
        verbose_name_plural = _("pagos")
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["organization", "-created_at"], name="payment_org_created")]
        constraints = [
            models.UniqueConstraint(fields=["provider_payment_id"], name="uniq_payment_provider_id"),
        ]

    def __str__(self):
        return f"{self._meta.verbose_name} {self.provider_payment_id}"


class ProviderEvent(TimeStampedModel):
    """Notificación del webhook ya recibida (idempotencia). De plataforma: solo ids y resultado."""

    class Result(models.TextChoices):
        PROCESSED = "processed", _("Procesado")
        IGNORED = "ignored", _("Ignorado")
        ERROR = "error", _("Error (se reintenta)")

    provider = models.CharField(_("proveedor"), max_length=20, default="mercadopago")
    event_key = models.CharField(_("id del evento"), max_length=150)
    topic = models.CharField(_("tipo"), max_length=60)
    resource_id = models.CharField(_("id del recurso"), max_length=64)
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("organización"),
    )
    result = models.CharField(_("resultado"), max_length=10, choices=Result.choices, default=Result.ERROR)
    detail = models.CharField(_("detalle"), max_length=200, blank=True)
    attempts = models.PositiveSmallIntegerField(_("intentos"), default=0)

    class Meta:
        verbose_name = _("evento del proveedor de pagos")
        verbose_name_plural = _("eventos del proveedor de pagos")
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["provider", "event_key"], name="uniq_provider_event"),
        ]

    def __str__(self):
        return f"{self.topic} {self.resource_id}"
