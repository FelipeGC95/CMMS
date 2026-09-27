"""Pantalla de tokens de API (X-05): solo administradores."""

import datetime

from django import forms
from django.contrib import messages
from django.core.paginator import Paginator
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.translation import gettext
from django.utils.translation import gettext_lazy as _
from django.views.decorators.http import require_POST

from apps.accounts.permissions import Perm
from apps.core.decorators import membership_required

from .models import ApiToken
from .services import create_token, revoke_token

EXPIRY_CHOICES = [
    ("", _("Nunca")),
    ("30", _("30 días")),
    ("90", _("90 días")),
    ("365", _("1 año")),
]


class ApiTokenForm(forms.Form):
    name = forms.CharField(
        label=_("Nombre"), max_length=80, help_text=_("Para reconocerlo, p. ej. «ERP» o «Telemetría».")
    )
    scope = forms.ChoiceField(label=_("Alcance"), choices=ApiToken.Scope.choices, initial=ApiToken.Scope.READ)
    expires_in = forms.ChoiceField(label=_("Vence"), choices=EXPIRY_CHOICES, required=False)


@membership_required(perm=Perm.API_TOKEN_MANAGE)
def api_token_list_view(request):
    form = ApiTokenForm(request.POST or None)
    raw_token = None
    if request.method == "POST" and form.is_valid():
        days = form.cleaned_data["expires_in"]
        _token, raw_token = create_token(
            membership=request.membership,
            name=form.cleaned_data["name"],
            scope=form.cleaned_data["scope"],
            expires_at=timezone.now() + datetime.timedelta(days=int(days)) if days else None,
        )
        form = ApiTokenForm()
    tokens = ApiToken.objects.select_related("created_by").order_by("revoked_at", "-created_at")
    page = Paginator(tokens, 25).get_page(request.GET.get("page"))
    return render(
        request,
        "api/token_list.html",
        {"form": form, "page": page, "raw_token": raw_token, "now": timezone.now()},
    )


@require_POST
@membership_required(perm=Perm.API_TOKEN_MANAGE)
def api_token_revoke_view(request, pk):
    token = get_object_or_404(ApiToken, pk=pk)
    revoke_token(membership=request.membership, token=token)
    messages.success(request, gettext("Token «%(name)s» revocado.") % {"name": token.name})
    return redirect("api_token_list")
