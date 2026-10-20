"""Cobro y autogestión (ORG-13): checkout, webhooks, impago y reactivación, cambio de plan y tarea diaria.

Sin red: el proveedor es `FakeGateway` (tests/billing_utils.py) y la UF se guarda en la tabla.
"""

import datetime
from decimal import Decimal

import pytest
from django.utils import timezone

from apps.assets.models import Asset
from apps.audit.models import AuditEvent
from apps.billing import services
from apps.billing.access import Access, Reason, billing_status
from apps.billing.gateway import Notification, RemoteSubscription
from apps.billing.models import BillingAccount, Payment, Period, ProviderEvent
from apps.billing.services import BillingError
from apps.core.tenancy import tenant_context
from apps.notifications.models import Kind
from apps.notifications.models import Notification as InAppNotification
from apps.organizations.models import Coupon, Organization, Plan
from tests.billing_utils import FakeGateway, authorize, install_gateway, make_billable, pay, plans, store_uf


@pytest.fixture
def gateway(monkeypatch, settings):
    return install_gateway(monkeypatch, settings)


@pytest.fixture
def billable(tenant_a):
    make_billable(tenant_a.org)
    tenant_a.org.refresh_from_db()
    tenant_a.admin.organization = tenant_a.org
    return tenant_a


def _org(tenant):
    return Organization.objects.get(pk=tenant.org.pk)


def _subscribe(tenant, gateway, plan_code="basic", period=Period.MONTHLY):
    with tenant_context(tenant.org):
        store_uf()
        url = services.start_checkout(
            membership=tenant.admin, plan=Plan.objects.get(code=plan_code), period=period
        )
    subscription_id = url.rsplit("/", 1)[1]
    authorize(gateway, subscription_id)
    return subscription_id


def test_checkout_amount_is_computed_on_the_server(billable, gateway):
    basic, _pro = plans()
    with tenant_context(billable.org):
        store_uf()
        url = services.start_checkout(membership=billable.admin, plan=basic, period=Period.ANNUAL)
        account = BillingAccount.objects.get()
    assert url.startswith("https://www.mercadopago.cl/checkout/")
    _kind, call = gateway.calls[0]
    assert call["amount_clp"] == 592500  # 15 UF × 39.500
    assert call["frequency_months"] == 12
    assert call["external_reference"] == account.external_reference
    assert call["payer_email"] == billable.admin.user.email
    assert account.status == BillingAccount.Status.PENDING and account.amount_uf == Decimal("15")
    assert _org(billable).plan.code == "trial"  # el plan cambia al autorizarse
    with tenant_context(billable.org):
        assert AuditEvent.objects.filter(target_id=str(account.pk), action="create").exists()


def test_checkout_without_provider_or_uf(billable, monkeypatch, settings):
    basic, _pro = plans()
    with tenant_context(billable.org), pytest.raises(BillingError, match="aún no está activo"):
        store_uf()
        services.start_checkout(membership=billable.admin, plan=basic, period=Period.MONTHLY)
    gateway = install_gateway(monkeypatch, settings)
    monkeypatch.setattr(services.uf, "uf_for_charge", lambda: None)
    with tenant_context(billable.org), pytest.raises(BillingError, match="valor de la UF"):
        services.start_checkout(membership=billable.admin, plan=basic, period=Period.MONTHLY)
    assert gateway.calls == []


def test_enterprise_is_quoted_unless_it_has_an_agreed_price(billable, gateway):
    enterprise = Plan.objects.get(code="enterprise")
    with tenant_context(billable.org), pytest.raises(BillingError, match="cotización"):
        store_uf()
        services.start_checkout(membership=billable.admin, plan=enterprise, period=Period.MONTHLY)
    Organization.objects.filter(pk=billable.org.pk).update(custom_monthly_uf=Decimal("12.5"))
    billable.admin.organization = _org(billable)
    with tenant_context(billable.admin.organization):
        services.start_checkout(membership=billable.admin, plan=enterprise, period=Period.MONTHLY)
    assert gateway.calls[-1][1]["amount_clp"] == 493750  # 12,5 UF


def test_coupon_halves_the_charge(billable, gateway):
    coupon = Coupon.objects.create(code="lanzamiento", name="Lanzamiento", percent_off=50, duration_months=12)
    Organization.objects.filter(pk=billable.org.pk).update(coupon=coupon, coupon_applied_at=timezone.now())
    billable.admin.organization = _org(billable)
    with tenant_context(billable.admin.organization):
        store_uf()
        services.start_checkout(
            membership=billable.admin, plan=Plan.objects.get(code="basic"), period="monthly"
        )
    assert gateway.calls[0][1]["amount_clp"] == 29625


def test_authorized_subscription_and_approved_payment_activate_the_plan(billable, gateway):
    subscription_id = _subscribe(billable, gateway)
    org = _org(billable)
    assert org.plan.code == "basic" and org.status == Organization.Status.ACTIVE
    assert pay(gateway, subscription_id, "pay-1") == ProviderEvent.Result.PROCESSED
    org = _org(billable)
    with tenant_context(org):
        payment = Payment.objects.get()
    assert payment.status == Payment.Status.APPROVED and payment.uf_value == Decimal("39500.00")
    assert payment.amount_uf == Decimal("1.5") and payment.amount_clp == 59250
    assert org.paid_through > timezone.now() + datetime.timedelta(days=28)
    assert billing_status(org).access == Access.ACTIVE


def test_repeated_event_is_processed_once(billable, gateway):
    subscription_id = _subscribe(billable, gateway)
    pay(gateway, subscription_id, "pay-1", key="evento-1")
    first = _org(billable).paid_through
    assert pay(gateway, subscription_id, "pay-1", key="evento-1") == "duplicate"
    # El mismo pago con otro id de evento tampoco extiende dos veces el acceso.
    assert pay(gateway, subscription_id, "pay-1", key="evento-2") == ProviderEvent.Result.PROCESSED
    assert _org(billable).paid_through == first
    with tenant_context(billable.org):
        assert Payment.objects.count() == 1


def test_subscription_of_another_organization_is_ignored(billable, tenant_b, gateway):
    subscription_id = _subscribe(billable, gateway)
    # Una suscripción con la referencia de B pero el id guardado en A: no se toca nada.
    with tenant_context(tenant_b.org):
        other = BillingAccount.objects.create()
    gateway.subscriptions[subscription_id] = RemoteSubscription(
        subscription_id, "cancelled", other.external_reference, 59250, None
    )
    result = services.handle_notification(
        notification=Notification("x:1", "subscription", subscription_id), gateway=gateway
    )
    assert result == ProviderEvent.Result.IGNORED
    with tenant_context(billable.org):
        assert BillingAccount.objects.get().status == BillingAccount.Status.AUTHORIZED
    # Un pago de una suscripción desconocida no crea nada en ninguna organización.
    assert pay(gateway, "pre-desconocida", "pay-9") == ProviderEvent.Result.IGNORED
    assert Payment.all_tenants.count() == 0


def test_rejected_payment_starts_grace_and_approval_reactivates(
    billable, gateway, django_capture_on_commit_callbacks
):
    subscription_id = _subscribe(billable, gateway)
    pay(gateway, subscription_id, "pay-1")
    with django_capture_on_commit_callbacks(execute=True):
        pay(gateway, subscription_id, "pay-2", status="rejected")
    org = _org(billable)
    assert org.past_due_since is not None
    status = billing_status(org)
    assert status.access == Access.GRACE and status.reason == Reason.PAYMENT_FAILED
    with tenant_context(org):
        notice = InAppNotification.objects.get(kind=Kind.BILLING)
    assert notice.recipient == billable.admin.user and "No se pudo cobrar" in notice.title
    later = max(org.past_due_since, org.paid_through) + datetime.timedelta(days=8)
    assert billing_status(org, now=later).access == Access.READ_ONLY
    pay(gateway, subscription_id, "pay-3")
    org = _org(billable)
    assert org.past_due_since is None and billing_status(org).access == Access.ACTIVE


def test_trial_grace_read_only_and_reactivation(billable, gateway):
    org = _org(billable)
    now = timezone.now()
    assert billing_status(org, now=now).access == Access.TRIAL
    ended = org.trial_ends_at + datetime.timedelta(days=1)
    status = billing_status(org, now=ended)
    assert status.access == Access.GRACE and status.reason == Reason.TRIAL_ENDED
    assert status.ends_at == org.trial_ends_at + datetime.timedelta(days=7)
    assert billing_status(org, now=org.trial_ends_at + datetime.timedelta(days=7, seconds=1)).read_only
    # Ya en solo lectura, paga y se reactiva sola.
    Organization.objects.filter(pk=org.pk).update(trial_ends_at=now - datetime.timedelta(days=20))
    billable.admin.organization = _org(billable)
    assert billing_status(_org(billable)).read_only
    subscription_id = _subscribe(billable, gateway)
    pay(gateway, subscription_id, "pay-1")
    assert billing_status(_org(billable)).access == Access.ACTIVE


def test_exempt_organizations_never_lock(tenant_a):
    org = _org(tenant_a)
    assert org.billing_exempt
    Organization.objects.filter(pk=org.pk).update(
        trial_ends_at=timezone.now() - datetime.timedelta(days=90),
        past_due_since=timezone.now() - datetime.timedelta(days=90),
    )
    assert billing_status(_org(tenant_a)).access == Access.EXEMPT


def test_deletion_grace_is_read_only_even_when_exempt(tenant_a):
    Organization.objects.filter(pk=tenant_a.org.pk).update(status=Organization.Status.PENDING_DELETION)
    status = billing_status(_org(tenant_a))
    assert status.read_only and status.reason == Reason.DELETION


def test_upgrade_is_immediate(billable, gateway):
    subscription_id = _subscribe(billable, gateway)
    _basic, pro = plans()
    billable.admin.organization = _org(billable)
    with tenant_context(billable.admin.organization):
        account = services.change_plan(membership=billable.admin, plan=pro)
    assert account.plan == pro and account.pending_plan is None
    assert ("update_amount", subscription_id, 158000) in gateway.calls  # 4 UF
    assert _org(billable).plan == pro


def test_downgrade_waits_for_the_period_end_and_respects_quotas(billable, gateway):
    _basic, pro = plans()
    _subscribe(billable, gateway, plan_code="pro")
    basic = Plan.objects.get(code="basic")
    billable.admin.organization = _org(billable)
    with tenant_context(billable.org):
        for i in range(basic.max_assets):
            Asset.objects.create(code=f"X-{i}", name=f"Equipo {i}", asset_class=billable.asset.asset_class)
        with pytest.raises(BillingError, match="No puedes bajar al plan Básico: Activos: usas 51"):
            services.change_plan(membership=billable.admin, plan=basic)
        Asset.objects.filter(code="X-0").delete()
        account = services.change_plan(membership=billable.admin, plan=basic)
    assert account.pending_plan == basic and _org(billable).plan == pro
    # La tarea diaria la aplica cuando falta un día o menos para el próximo cobro.
    with tenant_context(billable.org):
        BillingAccount.objects.update(next_charge_at=timezone.now() + datetime.timedelta(hours=12))
        services.update_subscription_amount()
        account = BillingAccount.objects.get()
    assert account.plan == basic and account.pending_plan is None
    assert _org(billable).plan == basic


def test_choosing_the_current_plan_undoes_a_scheduled_downgrade(billable, gateway):
    _subscribe(billable, gateway, plan_code="pro")
    basic, pro = plans()
    with tenant_context(billable.org):
        services.change_plan(membership=billable.admin, plan=basic)
        account = services.change_plan(membership=billable.admin, plan=pro)
    assert account.pending_plan is None


def test_cancel_keeps_access_until_the_paid_period_ends(billable, gateway):
    subscription_id = _subscribe(billable, gateway)
    pay(gateway, subscription_id, "pay-1")
    with tenant_context(billable.org):
        account = services.cancel_subscription(membership=billable.admin)
    assert account.status == BillingAccount.Status.CANCELLED
    assert ("cancel", subscription_id) in gateway.calls
    org = _org(billable)
    assert billing_status(org).access == Access.ACTIVE
    after = org.paid_through + datetime.timedelta(days=1)
    status = billing_status(org, now=after)
    assert status.access == Access.GRACE and status.reason == Reason.UNPAID


def test_daily_update_follows_the_uf(billable, gateway):
    subscription_id = _subscribe(billable, gateway)
    with tenant_context(billable.org):
        assert services.update_subscription_amount() is False  # misma UF: no llama al proveedor
        store_uf(Decimal("40000.00"))
        assert services.update_subscription_amount() is True
        account = BillingAccount.objects.get()
    assert ("update_amount", subscription_id, 60000) in gateway.calls
    assert account.amount_clp == 60000 and account.uf_value == Decimal("40000.00")


def test_daily_update_with_provider_down_keeps_the_amount(billable, gateway):
    _subscribe(billable, gateway)
    gateway.fail = True
    with tenant_context(billable.org):
        store_uf(Decimal("40000.00"))
        assert services.update_subscription_amount() is False
        assert BillingAccount.objects.get().amount_clp == 59250


def test_trial_notices_are_sent_once(billable, django_capture_on_commit_callbacks):
    with tenant_context(billable.org):
        assert services.send_billing_notices() == 0  # faltan 10 días
        Organization.objects.filter(pk=billable.org.pk).update(
            trial_ends_at=timezone.now() + datetime.timedelta(days=6)
        )
        assert services.send_billing_notices() == 1
        assert services.send_billing_notices() == 0
        Organization.objects.filter(pk=billable.org.pk).update(
            trial_ends_at=timezone.now() + datetime.timedelta(hours=20)
        )
        assert services.send_billing_notices() == 1
        titles = list(InAppNotification.objects.filter(kind=Kind.BILLING).values_list("title", flat=True))
        # Solo los administradores reciben el aviso.
        assert set(InAppNotification.objects.values_list("recipient", flat=True)) == {billable.admin.user_id}
    assert any("una semana" in t for t in titles) and any("mañana" in t for t in titles)


def test_run_daily_skips_exempt_organizations(billable, tenant_b, monkeypatch):
    monkeypatch.setattr(services.uf, "refresh_uf_value", lambda: None)
    Organization.objects.filter(pk=billable.org.pk).update(
        trial_ends_at=timezone.now() + datetime.timedelta(days=3)
    )
    totals = services.run_daily()
    assert totals["notices"] == 1
    with tenant_context(tenant_b.org):
        assert not BillingAccount.objects.exists()


def test_billing_details_validate_the_rut(billable):
    with tenant_context(billable.org):
        account = services.update_billing_details(
            membership=billable.admin,
            tax_id="76123456-0",
            legal_name=" Empresa A SpA ",
            business_activity="Alimentos",
            billing_address="Calle 1",
            billing_email="Pagos@Empresa.cl",
        )
        assert account.tax_id == "76.123.456-0" and account.billing_email == "pagos@empresa.cl"
        with pytest.raises(Exception, match="dígito verificador"):
            services.update_billing_details(
                membership=billable.admin,
                tax_id="76.123.456-1",
                legal_name="",
                business_activity="",
                billing_address="",
                billing_email="",
            )


def test_gateway_down_on_webhook_asks_for_a_retry(billable, gateway):
    subscription_id = _subscribe(billable, gateway)
    gateway.fail = True
    with pytest.raises(services.RetryLater):
        services.handle_notification(
            notification=Notification("k:1", "subscription", subscription_id), gateway=gateway
        )
    event = ProviderEvent.objects.get(event_key="k:1")
    assert event.result == ProviderEvent.Result.ERROR
    gateway.fail = False
    assert (
        services.handle_notification(
            notification=Notification("k:1", "subscription", subscription_id), gateway=gateway
        )
        == ProviderEvent.Result.PROCESSED
    )


def test_fake_gateway_is_isolated_per_test():
    assert FakeGateway().calls == []
