"""Facturación (ORG-13): autogestión del plan para administradores y webhook entrante del proveedor."""

import logging

from django.conf import settings
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.http import HttpResponse, HttpResponseBadRequest, JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.translation import gettext as _
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from apps.accounts.permissions import Perm
from apps.core.decorators import membership_required
from apps.organizations.models import Plan

from . import services
from .forms import BillingDetailsForm, ChangePlanForm, CheckoutForm
from .gateway import InvalidSignature, get_gateway
from .selectors import billing_overview, payment_history

logger = logging.getLogger(__name__)

PAYMENTS_PER_PAGE = 20
MAX_WEBHOOK_BYTES = 64 * 1024


def _error_text(error: ValidationError) -> str:
    return " ".join(error.messages)


@membership_required(perm=Perm.ORGANIZATION_MANAGE)
def billing_view(request):
    """Plan, estado, próximo cobro, cambio de plan, datos de facturación e historial de pagos."""
    membership = request.membership
    overview = billing_overview(membership=membership)
    account = overview.account
    initial = {}
    if account is not None:
        initial = {field: getattr(account, field) for field in services.DETAIL_FIELDS}
    form = BillingDetailsForm(request.POST if request.method == "POST" else None, initial=initial)
    if request.method == "POST" and form.is_valid():
        try:
            services.update_billing_details(membership=membership, **form.cleaned_data)
        except ValidationError as error:
            form.add_error(None, error)
        else:
            messages.success(request, _("Datos de facturación guardados."))
            return redirect(f"{reverse('billing')}#details")
    if request.GET.get("checkout"):
        messages.info(
            request,
            _(
                "Volviste de Mercado Pago. Cuando confirme el pago, el plan se activa solo "
                "(puede tardar unos minutos)."
            ),
        )
    page = Paginator(payment_history(membership=membership), PAYMENTS_PER_PAGE).get_page(
        request.GET.get("page")
    )
    return render(
        request,
        "billing/billing.html",
        {
            "overview": overview,
            "form": form,
            "page": page,
            "contact_email": settings.BILLING_CONTACT_EMAIL,
            "grace_days": settings.BILLING_GRACE_DAYS,
        },
    )


def _plan_from(form):
    return Plan.objects.filter(code=form.cleaned_data["plan"], is_active=True).first()


@membership_required(perm=Perm.ORGANIZATION_MANAGE)
@require_POST
def billing_checkout_view(request):
    """Contratar: crea la suscripción en Mercado Pago y redirige a su checkout."""
    form = CheckoutForm(request.POST)
    plan = _plan_from(form) if form.is_valid() else None
    if plan is None:
        messages.error(request, _("Elige un plan y un período."))
        return redirect("billing")
    try:
        url = services.start_checkout(
            membership=request.membership, plan=plan, period=form.cleaned_data["period"]
        )
    except ValidationError as error:
        messages.error(request, _error_text(error))
        return redirect("billing")
    if not url.startswith("https://"):
        messages.error(request, _("Mercado Pago devolvió un enlace no válido."))
        return redirect("billing")
    return redirect(url)


@membership_required(perm=Perm.ORGANIZATION_MANAGE)
@require_POST
def billing_change_view(request):
    form = ChangePlanForm(request.POST)
    plan = _plan_from(form) if form.is_valid() else None
    if plan is None:
        messages.error(request, _("Elige un plan."))
        return redirect("billing")
    try:
        account = services.change_plan(membership=request.membership, plan=plan)
    except ValidationError as error:
        messages.error(request, _error_text(error))
    else:
        if account.pending_plan_id:
            messages.success(
                request,
                _("Cambio a %(plan)s programado para el fin del período pagado.") % {"plan": plan.name},
            )
        else:
            messages.success(request, _("Tu plan ahora es %(plan)s.") % {"plan": account.plan.name})
    return redirect("billing")


@membership_required(perm=Perm.ORGANIZATION_MANAGE)
@require_POST
def billing_cancel_view(request):
    try:
        services.cancel_subscription(membership=request.membership)
    except ValidationError as error:
        messages.error(request, _error_text(error))
    else:
        messages.success(
            request, _("Suscripción cancelada. Sigues con tu plan hasta el fin del período pagado.")
        )
    return redirect("billing")


@csrf_exempt  # lo llama Mercado Pago, sin sesión: la autenticidad la da la firma x-signature
@require_POST
def mercadopago_webhook_view(request):
    """Webhook entrante (ORG-13 CA6). Firma válida → se consulta el recurso a la API → idempotente.

    Responde 200 si se procesó o se ignoró, 401 con firma inválida, 503 si la API del proveedor no
    respondió (Mercado Pago reintenta) y nunca 500 por datos inesperados.
    """
    gateway = get_gateway()
    if gateway is None:
        return JsonResponse({"status": "disabled"}, status=503)
    if len(request.body) > MAX_WEBHOOK_BYTES:
        return HttpResponseBadRequest()
    headers = {key.lower(): value for key, value in request.headers.items()}
    try:
        notification = gateway.parse_notification(
            query=request.GET.dict(), headers=headers, body=request.body
        )
    except InvalidSignature:
        return JsonResponse({"status": "invalid_signature"}, status=401)
    try:
        result = services.handle_notification(notification=notification, gateway=gateway)
    except services.RetryLater:
        return JsonResponse({"status": "retry"}, status=503)
    except Exception:  # datos inesperados: se registra y no se pide reintento infinito
        logger.exception("Webhook de Mercado Pago no procesado (%s)", notification.event_key)
        return HttpResponse(status=200)
    return JsonResponse({"status": result})
