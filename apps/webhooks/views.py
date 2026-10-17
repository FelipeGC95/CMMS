"""Pantalla de webhooks salientes (X-08): solo quien administra integraciones."""

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
    form = WebhookForm(request.POST or None)
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
    form = WebhookEditForm(request.POST or None, initial=initial)
    if request.method == "POST" and form.is_valid():
        saved = _save(
            form,
            lambda **data: services.update_endpoint(membership=request.membership, endpoint=endpoint, **data),
        )
        if saved is not None:
            messages.success(request, gettext("Webhook guardado."))
            return redirect("webhook_detail", pk=endpoint.pk)
    deliveries = selectors.delivery_list(membership=request.membership, endpoint=endpoint)
    page = Paginator(deliveries, 25).get_page(request.GET.get("page"))
    return render(request, "webhooks/detail.html", {"endpoint": endpoint, "form": form, "page": page})


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
