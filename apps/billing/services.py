"""Cobro recurrente y autogestión del plan (ORG-13, ADR-033).

Escrituras de la facturación: contratar (checkout del proveedor), cambiar de plan, cancelar, datos
de facturación, webhooks entrantes, actualización diaria del monto en UF y avisos. Los montos se
calculan siempre aquí (`pricing`), nunca llegan del navegador.

El proveedor se usa solo por `gateway.get_gateway()`; sin credenciales no hay cobro en línea.
"""

import datetime
import logging

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _

from apps.accounts.permissions import Perm, has_perm, require_perm
from apps.audit import services as audit
from apps.audit.models import AuditEvent
from apps.core.tenancy import require_current_organization, tenant_context
from apps.organizations.models import Organization
from apps.organizations.selectors import organization_quotas

from . import pricing, uf
from .access import Access, Reason, billing_status
from .gateway import TOPIC_PAYMENT, TOPIC_SUBSCRIPTION, GatewayError, get_gateway
from .models import PERIOD_MONTHS, BillingAccount, Payment, Period, ProviderEvent
from .rut import normalize_rut
from .selectors import asset_count, purchasable_plans

logger = logging.getLogger(__name__)

# Al aprobarse un pago el acceso se extiende hasta el fin del período más esta tolerancia: el
# cobro automático del proveedor puede llegar horas o un día después de la fecha.
RENEWAL_TOLERANCE = datetime.timedelta(days=2)
# Una baja de plan programada se aplica cuando falta esto o menos para el próximo cobro.
SCHEDULED_CHANGE_LEAD = datetime.timedelta(days=1)
ACCOUNT_FIELDS = ["plan", "period", "status", "provider_subscription_id", "amount_clp", "pending_plan"]
DETAIL_FIELDS = ["tax_id", "legal_name", "business_activity", "billing_address", "billing_email"]


class BillingError(ValidationError):
    """Error de facturación que se muestra a la persona."""


# --- Lecturas que usan los servicios -----------------------------------------------------------


def account_for(organization=None) -> BillingAccount:
    """Cuenta de facturación de la organización del contexto (se crea vacía la primera vez)."""
    organization = organization or require_current_organization()
    account = BillingAccount.objects.select_related("plan", "pending_plan").first()
    if account is None:
        account = BillingAccount.objects.create()
        account = BillingAccount.objects.select_related("plan", "pending_plan").get(pk=account.pk)
    return account


def quote_for(*, organization, plan, period, uf_value=None, now=None):
    try:
        return pricing.quote_for_organization(
            organization=organization,
            plan=plan,
            period=period,
            asset_count=asset_count(organization),
            uf_value=uf_value,
            now=now or timezone.now(),
        )
    except pricing.PriceUnavailable as error:
        raise BillingError(
            _("El plan %(plan)s no tiene precio definido. Escríbenos a %(email)s.")
            % {"plan": plan.name, "email": settings.BILLING_CONTACT_EMAIL}
        ) from error


def _gateway_or_error():
    gateway = get_gateway()
    if gateway is None:
        raise BillingError(
            _("El pago en línea aún no está activo. Escríbenos a %(email)s para contratar.")
            % {"email": settings.BILLING_CONTACT_EMAIL}
        )
    return gateway


def _uf_or_error():
    value = uf.uf_for_charge()
    if value is None:
        raise BillingError(
            _(
                "No tenemos el valor de la UF en este momento, así que no podemos calcular el cobro. "
                "Inténtalo más tarde."
            )
        )
    return value


def _back_url() -> str:
    return settings.SITE_URL.rstrip("/") + reverse("billing") + "?checkout=1"


def _period_label(period) -> str:
    return str(Period(period).label)


# --- Autogestión (ORG-13 CA3/CA4) ---------------------------------------------------------------


@transaction.atomic
def start_checkout(*, membership, plan, period) -> str:
    """Crea la suscripción en el proveedor con el monto calculado aquí y devuelve el enlace de pago.

    Solo sin una suscripción activa (para cambiar de plan con una activa: `change_plan`).
    """
    require_perm(membership, Perm.ORGANIZATION_MANAGE)
    if period not in PERIOD_MONTHS:
        raise BillingError(_("Elige un período válido."))
    organization = Organization.objects.select_for_update().get(pk=membership.organization_id)
    if plan not in purchasable_plans(organization):
        raise BillingError(_("Ese plan no se contrata en línea. Pide una cotización."))
    account = account_for(organization)
    account = BillingAccount.objects.select_for_update().get(pk=account.pk)
    if account.is_active_subscription:
        raise BillingError(_("Ya tienes una suscripción activa: usa «Cambiar de plan»."))
    gateway = _gateway_or_error()
    uf_row = _uf_or_error()
    offer = quote_for(organization=organization, plan=plan, period=period, uf_value=uf_row.value)
    if not offer.amount_clp:
        raise BillingError(_("El monto calculado no es válido. Escríbenos a soporte."))
    previous = account.provider_subscription_id if account.status == BillingAccount.Status.PENDING else ""
    try:
        checkout = gateway.create_subscription(
            reason=_("GALBUST CMMS · Plan %(plan)s (%(period)s)")
            % {"plan": plan.name, "period": _period_label(period).lower()},
            external_reference=account.external_reference,
            payer_email=settings.MERCADOPAGO_TEST_PAYER_EMAIL
            or account.billing_email
            or membership.user.email,
            amount_clp=offer.amount_clp,
            frequency_months=PERIOD_MONTHS[period],
            back_url=_back_url(),
        )
    except GatewayError as error:
        logger.warning("No se pudo crear la suscripción: %s", error)
        raise BillingError(_("Mercado Pago no respondió. Inténtalo de nuevo en unos minutos.")) from error
    if previous:  # un checkout anterior sin pagar ya no sirve
        try:
            gateway.cancel_subscription(previous)
        except GatewayError:
            logger.info("No se pudo cancelar el checkout anterior %s", previous)
    before = audit.snapshot(account, ACCOUNT_FIELDS)
    account.plan, account.period = plan, period
    account.status = BillingAccount.Status.PENDING
    account.provider = gateway.name
    account.provider_subscription_id = checkout.subscription_id
    account.checkout_url = checkout.checkout_url
    _set_amount(account, offer, uf_row.value)
    account.pending_plan = None
    account.save()
    audit.record(
        action=AuditEvent.Action.CREATE,
        target=account,
        membership=membership,
        changes=audit.diff(before, audit.snapshot(account, ACCOUNT_FIELDS)),
    )
    return checkout.checkout_url


def _set_amount(account, offer, uf_value) -> None:
    account.amount_clp = offer.amount_clp
    account.amount_uf = offer.total_uf
    account.uf_value = uf_value
    account.amount_updated_at = timezone.now()


def _quota_problems(membership, plan) -> list[str]:
    """Cupos de `plan` que la organización hoy supera (para bajar de plan)."""
    limits = [plan.max_assets, plan.max_users, plan.max_storage_mb]
    problems = []
    for row, limit in zip(organization_quotas(membership=membership), limits, strict=True):
        if limit is not None and row.used > limit:
            problems.append(
                _("%(label)s: usas %(used)s y el plan %(plan)s permite %(limit)s")
                % {"label": row.label, "used": row.used, "plan": plan.name, "limit": limit}
            )
    return problems


@transaction.atomic
def change_plan(*, membership, plan) -> BillingAccount:
    """Cambia el plan de una suscripción activa (ORG-13 CA4).

    Subir: inmediato (el plan rige ya; el monto nuevo se cobra desde el próximo ciclo, sin
    prorrateo). Bajar: se programa para el fin del período y solo si la organización cabe en los
    cupos del plan menor. Elegir el plan actual anula un cambio programado.
    """
    require_perm(membership, Perm.ORGANIZATION_MANAGE)
    organization = Organization.objects.select_for_update().get(pk=membership.organization_id)
    account_id = account_for(organization).pk
    account = BillingAccount.objects.select_for_update().select_related("plan").get(pk=account_id)
    if not account.is_active_subscription or account.plan is None:
        raise BillingError(_("No tienes una suscripción activa: elige un plan y págalo."))
    before = audit.snapshot(account, ACCOUNT_FIELDS)
    if plan.pk == account.plan_id:
        if account.pending_plan_id is None:
            raise BillingError(_("Ya estás en ese plan."))
        account.pending_plan = None
        account.save(update_fields=["pending_plan", "updated_at"])
        audit.record(
            action=AuditEvent.Action.UPDATE,
            target=account,
            membership=membership,
            changes=audit.diff(before, audit.snapshot(account, ACCOUNT_FIELDS)),
        )
        return account
    if plan not in purchasable_plans(organization):
        raise BillingError(_("Ese plan no se contrata en línea. Pide una cotización."))
    uf_row = _uf_or_error()
    current = quote_for(organization=organization, plan=account.plan, period=account.period)
    offer = quote_for(organization=organization, plan=plan, period=account.period, uf_value=uf_row.value)
    if offer.monthly_uf > current.monthly_uf:
        gateway = _gateway_or_error()
        try:
            gateway.update_subscription_amount(account.provider_subscription_id, offer.amount_clp)
        except GatewayError as error:
            logger.warning("No se pudo actualizar el monto al subir de plan: %s", error)
            raise BillingError(_("Mercado Pago no respondió. Inténtalo de nuevo en unos minutos.")) from error
        account.plan, account.pending_plan = plan, None
        _set_amount(account, offer, uf_row.value)
        account.save()
        organization.plan = plan
        organization.save(update_fields=["plan", "updated_at"])
    else:
        problems = _quota_problems(membership, plan)
        if problems:
            raise BillingError(
                _("No puedes bajar al plan %(plan)s: %(problems)s. Reduce el uso y vuelve a intentarlo.")
                % {"plan": plan.name, "problems": "; ".join(problems)}
            )
        account.pending_plan = plan
        account.save(update_fields=["pending_plan", "updated_at"])
    audit.record(
        action=AuditEvent.Action.UPDATE,
        target=account,
        membership=membership,
        changes=audit.diff(before, audit.snapshot(account, ACCOUNT_FIELDS)),
    )
    return account


@transaction.atomic
def cancel_subscription(*, membership) -> BillingAccount:
    """Cancela la renovación. El acceso sigue hasta el fin del período pagado (ORG-13 CA4)."""
    require_perm(membership, Perm.ORGANIZATION_MANAGE)
    account = BillingAccount.objects.select_for_update().get(pk=account_for().pk)
    if account.status not in (BillingAccount.Status.AUTHORIZED, BillingAccount.Status.PAUSED):
        raise BillingError(_("No hay una suscripción activa que cancelar."))
    gateway = _gateway_or_error()
    try:
        gateway.cancel_subscription(account.provider_subscription_id)
    except GatewayError as error:
        logger.warning("No se pudo cancelar la suscripción: %s", error)
        raise BillingError(_("Mercado Pago no respondió. Inténtalo de nuevo en unos minutos.")) from error
    before = audit.snapshot(account, ACCOUNT_FIELDS)
    account.status = BillingAccount.Status.CANCELLED
    account.pending_plan = None
    account.next_charge_at = None
    account.save(update_fields=["status", "pending_plan", "next_charge_at", "updated_at"])
    audit.record(
        action=AuditEvent.Action.TRANSITION,
        target=account,
        membership=membership,
        changes=audit.diff(before, audit.snapshot(account, ACCOUNT_FIELDS)),
    )
    return account


@transaction.atomic
def update_billing_details(
    *, membership, tax_id, legal_name, business_activity, billing_address, billing_email
) -> BillingAccount:
    """Datos de facturación (ORG-13 CA7). El RUT se valida con su dígito verificador."""
    require_perm(membership, Perm.ORGANIZATION_MANAGE)
    account = BillingAccount.objects.select_for_update().get(pk=account_for().pk)
    before = audit.snapshot(account, DETAIL_FIELDS)
    account.tax_id = normalize_rut(tax_id) if tax_id.strip() else ""
    account.legal_name = legal_name.strip()
    account.business_activity = business_activity.strip()
    account.billing_address = billing_address.strip()
    account.billing_email = billing_email.strip().lower()
    changes = audit.diff(before, audit.snapshot(account, DETAIL_FIELDS))
    if changes:
        account.save(update_fields=[*DETAIL_FIELDS, "updated_at"])
        audit.record(action=AuditEvent.Action.UPDATE, target=account, membership=membership, changes=changes)
    return account


# --- Webhooks entrantes (ORG-13 CA6) ------------------------------------------------------------


class RetryLater(Exception):
    """El proveedor no respondió al consultar el recurso: el webhook responde 503 y se reintenta."""


def handle_notification(*, notification, gateway) -> str:
    """Procesa una notificación ya firmada. Idempotente por id de evento.

    Nunca confía en el cuerpo: consulta el recurso a la API del proveedor y solo toca la
    organización dueña de esa suscripción. Devuelve el resultado guardado.
    """
    event, _created = ProviderEvent.objects.get_or_create(
        provider=gateway.name,
        event_key=notification.event_key,
        defaults={
            "topic": notification.detail or notification.topic,
            "resource_id": notification.resource_id,
        },
    )
    if event.result in (ProviderEvent.Result.PROCESSED, ProviderEvent.Result.IGNORED) and event.attempts:
        return "duplicate"
    event.attempts += 1
    try:
        if notification.topic == TOPIC_SUBSCRIPTION:
            remote = gateway.get_subscription(notification.resource_id)
            organization, detail = sync_subscription(remote=remote)
        elif notification.topic == TOPIC_PAYMENT:
            remote = gateway.get_payment(notification.detail, notification.resource_id)
            organization, detail = record_payment(remote=remote)
        else:
            organization, detail = None, "topic"
    except GatewayError as error:
        event.result, event.detail = ProviderEvent.Result.ERROR, str(error)[:200]
        event.save(update_fields=["result", "detail", "attempts", "updated_at"])
        raise RetryLater(str(error)) from error
    event.organization = organization
    event.result = ProviderEvent.Result.PROCESSED if organization else ProviderEvent.Result.IGNORED
    event.detail = detail[:200]
    event.save(update_fields=["organization", "result", "detail", "attempts", "updated_at"])
    return event.result


def _account_by_subscription(subscription_id: str):
    """Búsqueda previa al contexto: el id lo guardamos nosotros al crear el checkout.

    all_tenants: aún no sabemos de qué organización es (lo dice esta misma fila).
    """
    if not subscription_id:
        return None
    return (
        BillingAccount.all_tenants.select_related("organization")
        .filter(provider_subscription_id=subscription_id)
        .first()
    )


def sync_subscription(*, remote) -> tuple[Organization | None, str]:
    """Aplica el estado de una suscripción consultada al proveedor a SU organización."""
    account = _account_by_subscription(remote.id)
    if account is None:
        return None, "unknown_subscription"
    if remote.external_reference != account.external_reference:
        # La suscripción no es de la organización que la guardó: no se toca nada.
        logger.warning("Suscripción %s con referencia ajena; se ignora", remote.id)
        return None, "reference_mismatch"
    organization = account.organization
    with tenant_context(organization), transaction.atomic():
        account = BillingAccount.objects.select_for_update().select_related("plan").get(pk=account.pk)
        organization = Organization.objects.select_for_update().get(pk=organization.pk)
        before = audit.snapshot(account, ACCOUNT_FIELDS)
        status_map = {
            "pending": BillingAccount.Status.PENDING,
            "authorized": BillingAccount.Status.AUTHORIZED,
            "paused": BillingAccount.Status.PAUSED,
            "cancelled": BillingAccount.Status.CANCELLED,
        }
        new_status = status_map.get(remote.status)
        if new_status is None:
            return organization, "unknown_status"
        account.status = new_status
        if remote.next_charge_at:
            account.next_charge_at = remote.next_charge_at
        if new_status == BillingAccount.Status.CANCELLED:
            account.next_charge_at = None
            account.pending_plan = None
        account.save()
        if new_status == BillingAccount.Status.AUTHORIZED and account.plan_id:
            _activate(organization, account.plan)
        changes = audit.diff(before, audit.snapshot(account, ACCOUNT_FIELDS))
        if changes:
            audit.record(action=AuditEvent.Action.TRANSITION, target=account, changes=changes)
    return organization, f"subscription_{remote.status}"


def _activate(organization, plan) -> None:
    fields = []
    if organization.plan_id != plan.pk:
        organization.plan = plan
        fields.append("plan")
    if organization.status == Organization.Status.TRIAL:
        organization.status = Organization.Status.ACTIVE
        fields.append("status")
    if fields:
        organization.save(update_fields=[*fields, "updated_at"])


def record_payment(*, remote) -> tuple[Organization | None, str]:
    """Registra un cobro consultado al proveedor; si se aprueba, extiende el acceso y reactiva."""
    if not remote.id or not remote.status:
        return None, "payment_not_ready"
    account = _account_by_subscription(remote.subscription_id)
    if account is None:
        return None, "unknown_subscription"
    # all_tenants: un id de pago es de una sola organización en toda la plataforma.
    elsewhere = Payment.all_tenants.filter(provider_payment_id=remote.id).exclude(
        organization=account.organization
    )
    if elsewhere.exists():
        logger.warning("Pago %s ya registrado en otra organización; se ignora", remote.id)
        return None, "payment_of_other_organization"
    organization = account.organization
    with tenant_context(organization), transaction.atomic():
        organization = Organization.objects.select_for_update().get(pk=organization.pk)
        account = BillingAccount.objects.select_for_update().select_related("plan").get(pk=account.pk)
        payment = Payment.objects.select_for_update().filter(provider_payment_id=remote.id).first()
        was_approved = payment is not None and payment.status == Payment.Status.APPROVED
        if payment is None:
            payment = Payment(
                account=account,
                provider_payment_id=remote.id,
                amount_clp=remote.amount_clp,
                amount_uf=account.amount_uf,
                uf_value=account.uf_value,
                plan=account.plan,
                period=account.period,
            )
        payment.status = remote.status
        payment.status_detail = remote.status_detail
        payment.amount_clp = remote.amount_clp or payment.amount_clp
        payment.paid_at = remote.paid_at or payment.paid_at or timezone.now()
        if payment.status == Payment.Status.APPROVED and not was_approved:
            payment.period_end = _extend_access(organization, account, payment)
        payment.save()
        if payment.status == Payment.Status.REJECTED and organization.past_due_since is None:
            organization.past_due_since = timezone.now()
            organization.save(update_fields=["past_due_since", "updated_at"])
            notify_payment_failed(organization=organization)
        audit.record(
            action=AuditEvent.Action.PAYMENT,
            target=payment,
            changes={"status": payment.status, "amount_clp": payment.amount_clp},
        )
    return organization, f"payment_{remote.status}"


def _extend_access(organization, account, payment):
    """Pago aprobado: el acceso llega al fin del período (+ tolerancia) y se borra el impago."""
    months = PERIOD_MONTHS.get(account.period, 1)
    start = payment.paid_at
    if organization.paid_through and organization.paid_through - RENEWAL_TOLERANCE > start:
        start = organization.paid_through - RENEWAL_TOLERANCE  # pago adelantado: se suma al período vigente
    period_end = pricing.add_months(start, months)
    organization.paid_through = period_end + RENEWAL_TOLERANCE
    organization.past_due_since = None
    fields = ["paid_through", "past_due_since"]
    if account.plan_id and organization.plan_id != account.plan_id:
        organization.plan = account.plan
        fields.append("plan")
    if organization.status == Organization.Status.TRIAL:
        organization.status = Organization.Status.ACTIVE
        fields.append("status")
    organization.save(update_fields=[*fields, "updated_at"])
    if account.status in (BillingAccount.Status.NONE, BillingAccount.Status.PENDING):
        account.status = BillingAccount.Status.AUTHORIZED
        account.save(update_fields=["status", "updated_at"])
    return period_end


# --- Tareas diarias (ORG-13 CA3, CA5) -----------------------------------------------------------


def update_subscription_amount(*, now=None) -> bool:
    """Recalcula el monto con la UF del día (y los tramos de activos) y lo actualiza en el proveedor.

    Corre en la organización del contexto. También aplica la baja de plan programada cuando falta un
    día o menos para el próximo cobro. Devuelve True si cambió el monto.
    """
    now = now or timezone.now()
    organization = require_current_organization()
    account = BillingAccount.objects.select_related("plan", "pending_plan").first()
    if account is None or not account.is_active_subscription or account.plan is None:
        return False
    gateway = get_gateway()
    uf_row = uf.current_uf_value(today=uf.chile_today(now))
    if gateway is None or uf_row is None:
        logger.warning("Sin proveedor o sin UF: no se actualiza el monto de %s", organization.pk)
        return False
    plan = account.plan
    apply_change = (
        account.pending_plan is not None
        and account.next_charge_at is not None
        and account.next_charge_at - now <= SCHEDULED_CHANGE_LEAD
    )
    if apply_change:
        plan = account.pending_plan
    offer = quote_for(
        organization=organization, plan=plan, period=account.period, uf_value=uf_row.value, now=now
    )
    if offer.amount_clp == account.amount_clp and not apply_change:
        return False
    try:
        if offer.amount_clp != account.amount_clp:
            gateway.update_subscription_amount(account.provider_subscription_id, offer.amount_clp)
    except GatewayError as error:
        logger.warning("No se pudo actualizar el monto de %s: %s", organization.pk, error)
        return False
    with transaction.atomic():
        before = audit.snapshot(account, ACCOUNT_FIELDS)
        changed = offer.amount_clp != account.amount_clp
        _set_amount(account, offer, uf_row.value)
        if apply_change:
            account.plan, account.pending_plan = plan, None
            Organization.objects.filter(pk=organization.pk).update(plan=plan, updated_at=now)
        account.save()
        audit.record(
            action=AuditEvent.Action.UPDATE,
            target=account,
            changes=audit.diff(before, audit.snapshot(account, ACCOUNT_FIELDS)),
        )
    return changed


def send_billing_notices(*, now=None) -> int:
    """Avisos (in-app y correo, X-02) del fin de la prueba (7 y 1 días antes), de la gracia y de la
    solo lectura. Uno por hito: se recuerdan en `BillingAccount.notices`. Organización del contexto."""
    now = now or timezone.now()
    organization = Organization.objects.get(pk=require_current_organization().pk)
    status = billing_status(organization, now=now)
    key, title, body = None, "", ""
    if status.access == Access.TRIAL and status.ends_at:
        left = status.ends_at - now
        date = timezone.localtime(status.ends_at).strftime("%d-%m-%Y")
        if left <= datetime.timedelta(days=1):
            key = "trial_1"
            title = _("Tu prueba de GALBUST CMMS termina mañana")
        elif left <= datetime.timedelta(days=7):
            key = "trial_7"
            title = _("Tu prueba de GALBUST CMMS termina en una semana")
        body = _("La prueba termina el %(date)s. Elige un plan en Facturación para seguir sin cortes.") % {
            "date": date
        }
    elif status.access == Access.GRACE and status.reason != Reason.PAYMENT_FAILED and status.ends_at:
        date = timezone.localtime(status.ends_at).strftime("%d-%m-%Y")
        key = f"grace:{status.ends_at.date().isoformat()}"
        title = _("Tu organización quedará en solo lectura el %(date)s") % {"date": date}
        body = _("Paga en Facturación antes de esa fecha para seguir creando y editando.")
    elif status.access == Access.READ_ONLY and status.reason != Reason.DELETION:
        since = organization.past_due_since or organization.paid_through or organization.trial_ends_at or now
        key = f"read_only:{since.date().isoformat()}"
        title = _("Tu organización está en solo lectura")
        body = _("Puedes ver y exportar tus datos. Paga en Facturación y se reactiva sola.")
    if key is None:
        return 0
    account = account_for(organization)
    if key in (account.notices or {}):
        return 0
    with transaction.atomic():
        count = _notify_admins(title=title, body=body)
        account.notices = {**(account.notices or {}), key: now.isoformat()}
        account.save(update_fields=["notices", "updated_at"])
    return count


def notify_payment_failed(*, organization) -> None:
    def run():
        with tenant_context(organization):
            _notify_admins(
                title=_("No se pudo cobrar tu suscripción de GALBUST CMMS"),
                body=_(
                    "Mercado Pago rechazó el cobro y lo reintentará. Revisa tu medio de pago: tienes "
                    "%(days)s días antes de que la organización quede en solo lectura."
                )
                % {"days": settings.BILLING_GRACE_DAYS},
            )

    transaction.on_commit(run, robust=True)


def _notify_admins(*, title, body) -> int:
    from apps.notifications.models import Kind
    from apps.notifications.services import active_memberships, deliver

    admins = [m for m in active_memberships() if has_perm(m, Perm.ORGANIZATION_MANAGE)]
    return len(deliver(kind=Kind.BILLING, memberships=admins, title=title, body=body, url=reverse("billing")))


def run_daily(*, now=None) -> dict:
    """Comando y tarea diaria: UF del día y, por organización con cobro en línea, monto y avisos.

    Código de plataforma: recorre organizaciones y entra al contexto de cada una.
    """
    now = now or timezone.now()
    refreshed = uf.refresh_uf_value()
    totals = {"uf": str(refreshed.value) if refreshed else "", "amounts": 0, "notices": 0}
    organizations = Organization.objects.filter(is_active=True, billing_exempt=False).exclude(
        status__in=[Organization.Status.DELETED, Organization.Status.PENDING_DELETION]
    )
    for organization in organizations:
        with tenant_context(organization):
            try:
                totals["amounts"] += int(update_subscription_amount(now=now))
                totals["notices"] += send_billing_notices(now=now)
            except Exception:  # una organización con problemas no frena a las demás
                logger.exception("Facturación diaria falló en %s", organization.pk)
    return totals
