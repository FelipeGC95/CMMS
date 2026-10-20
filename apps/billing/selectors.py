"""Lecturas de la facturación (ORG-13): resumen del plan y del próximo cobro, opciones e historial."""

from dataclasses import dataclass, field

from django.utils import timezone

from apps.accounts.permissions import Perm, require_perm
from apps.organizations.models import Plan

from . import pricing, uf
from .access import BillingStatus, billing_status
from .gateway import is_configured
from .models import BillingAccount, Payment, Period


def asset_count(organization) -> int:
    from apps.assets.models import Asset  # diferido: assets depende de organizations

    # Igual que el cupo (ORG-10): cuentan todos los activos, también los dados de baja.
    return Asset.objects.filter(organization=organization).count()


def purchasable_plans(organization) -> list[Plan]:
    """Planes que la organización puede contratar en línea: los de autoservicio con precio y, si el
    superadmin le fijó un precio acordado, también los cotizados (Empresa)."""
    plans = sorted(
        Plan.objects.filter(is_active=True),
        key=lambda plan: (plan.monthly_price_uf is None, plan.monthly_price_uf or 0, plan.name),
    )
    result = []
    for plan in plans:
        if plan.is_self_service and plan.monthly_price_uf is not None:
            result.append(plan)
        elif not plan.is_self_service and plan.code != "trial" and organization.custom_monthly_uf is not None:
            if organization.plan_id == plan.pk or plan.code == "enterprise":
                result.append(plan)
    return result


@dataclass
class PlanOption:
    plan: Plan
    monthly: pricing.Quote
    annual: pricing.Quote
    is_current: bool = False


@dataclass
class BillingOverview:
    status: BillingStatus
    account: BillingAccount | None
    plan: Plan | None
    uf_value: object  # UfValue o None
    next_charge: pricing.Quote | None
    options: list[PlanOption] = field(default_factory=list)
    quoted_plans: list[Plan] = field(default_factory=list)  # se cotizan (Empresa)
    online_payments: bool = False
    discount_percent: int = 0


def billing_overview(*, membership) -> BillingOverview:
    """Estado, plan, próximo cobro (UF y CLP estimado) y planes contratables, sin llamar a la red."""
    require_perm(membership, Perm.ORGANIZATION_MANAGE)
    organization = membership.organization
    now = timezone.now()
    account = BillingAccount.objects.select_related("plan", "pending_plan").first()
    uf_row = uf.current_uf_value()
    uf_value = uf_row.value if uf_row else None
    assets = asset_count(organization)
    discount = pricing.coupon_percent(organization, now=now)

    def offer(plan, period):
        try:
            return pricing.quote_for_organization(
                organization=organization,
                plan=plan,
                period=period,
                asset_count=assets,
                uf_value=uf_value,
                now=now,
            )
        except pricing.PriceUnavailable:
            return None

    contracted = account.plan if account and account.plan_id else None
    next_charge = None
    if account and account.is_active_subscription and contracted:
        next_charge = offer(account.pending_plan or contracted, account.period)
    options = []
    for plan in purchasable_plans(organization):
        monthly, annual = offer(plan, Period.MONTHLY), offer(plan, Period.ANNUAL)
        if monthly and annual:
            options.append(PlanOption(plan, monthly, annual, is_current=contracted == plan))
    purchasable = {option.plan.pk for option in options}
    quoted = [
        plan
        for plan in Plan.objects.filter(is_active=True, is_self_service=False).exclude(code="trial")
        if plan.pk not in purchasable
    ]
    return BillingOverview(
        status=billing_status(organization, now=now),
        account=account,
        plan=organization.plan,
        uf_value=uf_row,
        next_charge=next_charge,
        options=options,
        quoted_plans=quoted,
        online_payments=is_configured(),
        discount_percent=discount,
    )


def payment_history(*, membership):
    """Pagos de la organización, del más reciente al más antiguo (la vista pagina)."""
    require_perm(membership, Perm.ORGANIZATION_MANAGE)
    return Payment.objects.select_related("plan").order_by("-created_at")
