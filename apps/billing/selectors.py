"""Lecturas de la facturación (ORG-13): resumen del plan y del próximo cobro, opciones e historial."""

from dataclasses import dataclass, field

from django.db.models import F
from django.utils import timezone

from apps.accounts.permissions import Perm, require_perm
from apps.organizations.models import Plan
from apps.organizations.modules import PlanFeature, modules_lost, plan_features, plan_includes

from . import pricing, uf
from .access import Access, BillingStatus, billing_status
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
class FeatureCell:
    feature: PlanFeature
    included: bool


@dataclass
class PlanColumn:
    """Un plan en la comparación (tarjeta en móvil, columna en la tabla de escritorio).

    `kind`: «trial» (la prueba en curso, de referencia), «option» (contratable, con `option`) o
    «quoted» (se cotiza: Empresa sin precio acordado). `lost`: módulos del plan actual de la
    organización que este plan no trae (se avisan antes de cambiarse a él).
    """

    plan: Plan
    kind: str
    cells: list[FeatureCell]
    option: PlanOption | None = None
    is_current: bool = False
    lost: list[str] = field(default_factory=list)

    @property
    def included(self) -> list[PlanFeature]:
        return [cell.feature for cell in self.cells if cell.included]

    @property
    def excluded(self) -> list[PlanFeature]:
        return [cell.feature for cell in self.cells if not cell.included]

    @property
    def has_assistant(self) -> bool:
        return plan_includes(self.plan, "assistant")


def plan_column(plan, *, kind, rows, option=None, is_current=False, current_plan=None) -> PlanColumn:
    """Qué trae `plan` según `Plan.features` (vacío = todo) en el orden de `rows`."""
    return PlanColumn(
        plan=plan,
        kind=kind,
        cells=[FeatureCell(row, plan_includes(plan, row.module)) for row in rows],
        option=option,
        is_current=is_current,
        lost=[] if is_current or current_plan is None else modules_lost(current_plan, plan),
    )


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
    # Comparación (mismas filas en el mismo orden para cada plan): prueba, contratables y cotizados.
    features: list[PlanFeature] = field(default_factory=list)
    columns: list[PlanColumn] = field(default_factory=list)

    @property
    def feature_rows(self) -> list[tuple[PlanFeature, list[tuple[PlanColumn, bool]]]]:
        """Filas de la tabla comparativa: cada función con (plan, ¿incluida?) por columna."""
        return [
            (feature, [(column, column.cells[index].included) for column in self.columns])
            for index, feature in enumerate(self.features)
        ]


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
    subscribed = bool(account and account.is_active_subscription)
    next_charge = None
    if account and account.is_active_subscription and contracted:
        next_charge = offer(account.pending_plan or contracted, account.period)
    options = []
    for plan in purchasable_plans(organization):
        monthly, annual = offer(plan, Period.MONTHLY), offer(plan, Period.ANNUAL)
        if monthly and annual:
            options.append(PlanOption(plan, monthly, annual, is_current=subscribed and contracted == plan))
    purchasable = {option.plan.pk for option in options}
    quoted = [
        plan
        for plan in Plan.objects.filter(is_active=True, is_self_service=False).exclude(code="trial")
        if plan.pk not in purchasable
    ]
    status = billing_status(organization, now=now)
    rows = plan_features()
    current_plan = organization.plan
    columns = []
    # La prueba va de referencia mientras dura y su plan no es uno de los que se venden.
    offered = purchasable | {plan.pk for plan in quoted}
    if (
        status.access == Access.TRIAL
        and not subscribed
        and current_plan is not None
        and current_plan.pk not in offered
    ):
        columns.append(plan_column(current_plan, kind="trial", rows=rows, is_current=True))
    columns += [
        plan_column(
            option.plan,
            kind="option",
            rows=rows,
            option=option,
            is_current=option.is_current,
            current_plan=current_plan,
        )
        for option in options
    ]
    columns += [plan_column(plan, kind="quoted", rows=rows, current_plan=current_plan) for plan in quoted]
    return BillingOverview(
        status=status,
        account=account,
        plan=organization.plan,
        uf_value=uf_row,
        next_charge=next_charge,
        options=options,
        quoted_plans=quoted,
        online_payments=is_configured(),
        discount_percent=discount,
        features=rows,
        columns=columns,
    )


def payment_history(*, membership):
    """Pagos de la organización, del más reciente al más antiguo (la vista pagina)."""
    require_perm(membership, Perm.ORGANIZATION_MANAGE)
    return Payment.objects.select_related("plan").order_by(F("paid_at").desc(nulls_last=True), "-created_at")
