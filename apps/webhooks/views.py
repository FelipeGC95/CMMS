"""Pantalla de webhooks salientes (X-08): solo quien administra integraciones."""

import json

from django import forms
from django.contrib import messages
from django.core.paginator import Paginator
from django.shortcuts import redirect, render
from django.utils.translation import gettext
from django.utils.translation import gettext_lazy as _
from django.views.decorators.http import require_POST

from apps.accounts.permissions import Perm
from apps.core.decorators import membership_required

from . import selectors, services
from .models import Event


class WebhookForm(forms.Form):
    """Alta y edición. Solo se ofrecen los eventos que el plan permite; al editar, los ya
    suscritos se conservan aunque el plan ya no los ofrezca."""

    name = forms.CharField(label=_("Nombre"), max_length=80, help_text=_("Para reconocerlo, p. ej. «ERP»."))
    url = forms.URLField(
        label=_("URL"),
        max_length=500,
        assume_scheme="https",
        help_text=_("Debe ser https y pública. Recibe un POST con JSON firmado."),
    )
    events = forms.MultipleChoiceField(
        label=_("Eventos"), choices=Event.choices, widget=forms.CheckboxSelectMultiple
    )

    def __init__(self, *args, organization=None, keep=(), **kwargs):
        super().__init__(*args, **kwargs)
        offered = set(services.enabled_events(organization)) | set(keep)
        self.fields["events"].choices = [(code, label) for code, label in Event.choices if code in offered]


class WebhookEditForm(WebhookForm):
    is_active = forms.BooleanField(label=_("Activo"), required=False)


def _save(form, call):
    """Llama al servicio; sus errores de validación quedan en el formulario."""
    try:
        return call(**form.cleaned_data)
    except services.WebhookError as error:
        field = "url" if isinstance(error, services.UnsafeURLError) else "events"
        form.add_error(field, error.message)
        return None


@membership_required(perm=Perm.API_TOKEN_MANAGE)
def webhook_list_view(request):
    form = WebhookForm(request.POST or None, organization=request.organization)
    if request.method == "POST" and form.is_valid():
        endpoint = _save(form, lambda **data: services.create_endpoint(membership=request.membership, **data))
        if endpoint is not None:
            messages.success(request, gettext("Webhook «%(name)s» creado.") % {"name": endpoint.name})
            return redirect("webhook_detail", pk=endpoint.pk)
    page = Paginator(selectors.endpoint_list(membership=request.membership), 25).get_page(
        request.GET.get("page")
    )
    return render(request, "webhooks/list.html", {"form": form, "page": page})


@membership_required(perm=Perm.API_TOKEN_MANAGE)
def webhook_detail_view(request, pk):
    endpoint = selectors.get_endpoint(membership=request.membership, pk=pk)
    initial = {
        "name": endpoint.name,
        "url": endpoint.url,
        "events": endpoint.events,
        "is_active": endpoint.is_active,
    }
    form = WebhookEditForm(
        request.POST or None, initial=initial, organization=request.organization, keep=endpoint.events
    )
    if request.method == "POST" and form.is_valid():
        saved = _save(
            form,
            lambda **data: services.update_endpoint(membership=request.membership, endpoint=endpoint, **data),
        )
        if saved is not None:
            messages.success(request, gettext("Webhook guardado."))
            return redirect("webhook_detail", pk=endpoint.pk)
    return _render_detail(request, endpoint, form)


def _render_detail(request, endpoint, form, new_secret=None):
    deliveries = selectors.delivery_list(membership=request.membership, endpoint=endpoint)
    page = Paginator(deliveries, 25).get_page(request.GET.get("page"))
    return render(
        request,
        "webhooks/detail.html",
        {"endpoint": endpoint, "form": form, "page": page, "new_secret": new_secret},
    )


@require_POST
@membership_required(perm=Perm.API_TOKEN_MANAGE)
def webhook_secret_view(request, pk):
    """Regenera el secreto y responde con el detalle mostrando el nuevo (como al crear un token)."""
    endpoint = selectors.get_endpoint(membership=request.membership, pk=pk)
    new_secret = services.regenerate_secret(
        membership=request.membership,
        endpoint=endpoint,
        keep_previous=request.POST.get("revoke_previous") != "1",
    )
    endpoint.refresh_from_db()
    form = WebhookEditForm(
        initial={
            "name": endpoint.name,
            "url": endpoint.url,
            "events": endpoint.events,
            "is_active": endpoint.is_active,
        },
        organization=request.organization,
        keep=endpoint.events,
    )
    return _render_detail(request, endpoint, form, new_secret=new_secret)


@require_POST
@membership_required(perm=Perm.API_TOKEN_MANAGE)
def webhook_previous_secret_view(request, pk):
    """Termina la ventana de traslape: el secreto anterior deja de firmar de inmediato."""
    endpoint = selectors.get_endpoint(membership=request.membership, pk=pk)
    services.discard_previous_secret(membership=request.membership, endpoint=endpoint)
    messages.success(request, gettext("El secreto anterior dejó de valer."))
    return redirect("webhook_detail", pk=endpoint.pk)


@membership_required(perm=Perm.API_TOKEN_MANAGE)
def webhook_delivery_view(request, pk, delivery_pk):
    """Detalle de una entrega: el cuerpo enviado y el código y la respuesta (truncada)."""
    endpoint = selectors.get_endpoint(membership=request.membership, pk=pk)
    delivery = selectors.get_delivery(membership=request.membership, endpoint=endpoint, pk=delivery_pk)
    return render(
        request,
        "webhooks/delivery.html",
        {
            "endpoint": endpoint,
            "delivery": delivery,
            "body": json.dumps(delivery.payload, ensure_ascii=False, indent=2),
            "redeliveries": selectors.redelivery_list(membership=request.membership, delivery=delivery),
        },
    )


@require_POST
@membership_required(perm=Perm.API_TOKEN_MANAGE)
def webhook_redeliver_view(request, pk, delivery_pk):
    endpoint = selectors.get_endpoint(membership=request.membership, pk=pk)
    delivery = selectors.get_delivery(membership=request.membership, endpoint=endpoint, pk=delivery_pk)
    try:
        copy = services.redeliver(membership=request.membership, delivery=delivery)
    except services.WebhookError as error:
        messages.error(request, error.message)
        return redirect("webhook_delivery", pk=endpoint.pk, delivery_pk=delivery.pk)
    messages.success(request, gettext("Entrega reenviada con el mismo ID de evento."))
    return redirect("webhook_delivery", pk=endpoint.pk, delivery_pk=copy.pk)


@require_POST
@membership_required(perm=Perm.API_TOKEN_MANAGE)
def webhook_test_view(request, pk):
    endpoint = selectors.get_endpoint(membership=request.membership, pk=pk)
    try:
        services.send_test(membership=request.membership, endpoint=endpoint)
    except services.WebhookError as error:
        messages.error(request, error.message)
    else:
        messages.success(request, gettext("Prueba enviada. Su resultado aparece en «Últimas entregas»."))
    return redirect("webhook_detail", pk=endpoint.pk)
