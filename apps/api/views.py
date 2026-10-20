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
from apps.organizations.modules import has_module

from .models import ApiToken
from .scopes import ALL, ALL_READ, READ, SCOPE_GROUPS, WRITE
from .services import create_token, revoke_token

EXPIRY_CHOICES = [
    ("", _("Nunca")),
    ("30", _("30 días")),
    ("90", _("90 días")),
    ("365", _("1 año")),
]


def _scope_choices():
    groups = [(ALL, _("Todo (también lo que se agregue a la API)"))] + [
        (g.key, g.label) for g in SCOPE_GROUPS
    ]
    return [(f"{key}:{mode}", f"{label} · {mode}") for key, label in groups for mode in (READ, WRITE)]


class ApiTokenForm(forms.Form):
    name = forms.CharField(
        label=_("Nombre"), max_length=80, help_text=_("Para reconocerlo, p. ej. «ERP» o «Telemetría».")
    )
    scopes = forms.MultipleChoiceField(
        label=_("Alcances"),
        choices=_scope_choices,
        initial=[ALL_READ],
        widget=forms.CheckboxSelectMultiple,
        error_messages={"required": _("Elige al menos un alcance.")},
    )
    expires_in = forms.ChoiceField(label=_("Vence"), choices=EXPIRY_CHOICES, required=False)


def scope_rows(form, organization) -> list[dict]:
    """Filas de la matriz de alcances: el grupo y sus casillas de lectura y escritura.

    Los grupos de módulos que el plan no incluye no se ofrecen.
    """
    widgets = {widget.data["value"]: widget for widget in form["scopes"]}
    groups = [(ALL, _("Todo"), _("Todos los recursos, también los que se agreguen a la API."))]
    groups += [(g.key, g.label, "") for g in SCOPE_GROUPS if has_module(organization, g.module)]
    return [
        {
            "label": label,
            "help": help_text,
            "read": widgets[f"{key}:{READ}"],
            "write": widgets[f"{key}:{WRITE}"],
        }
        for key, label, help_text in groups
    ]


@membership_required(perm=Perm.API_TOKEN_MANAGE)
def api_token_list_view(request):
    form = ApiTokenForm(request.POST or None)
    raw_token = None
    if request.method == "POST" and form.is_valid():
        days = form.cleaned_data["expires_in"]
        _token, raw_token = create_token(
            membership=request.membership,
            name=form.cleaned_data["name"],
            scopes=form.cleaned_data["scopes"],
            expires_at=timezone.now() + datetime.timedelta(days=int(days)) if days else None,
        )
        form = ApiTokenForm()
    tokens = ApiToken.objects.select_related("created_by").order_by("revoked_at", "-created_at")
    page = Paginator(tokens, 25).get_page(request.GET.get("page"))
    return render(
        request,
        "api/token_list.html",
        {
            "form": form,
            "scope_rows": scope_rows(form, request.organization),
            "page": page,
            "raw_token": raw_token,
            "now": timezone.now(),
        },
    )


@require_POST
@membership_required(perm=Perm.API_TOKEN_MANAGE)
def api_token_revoke_view(request, pk):
    token = get_object_or_404(ApiToken, pk=pk)
    revoke_token(membership=request.membership, token=token)
    messages.success(request, gettext("Token «%(name)s» revocado.") % {"name": token.name})
    return redirect("api_token_list")
