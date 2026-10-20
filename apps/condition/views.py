"""Pantallas web de mantenimiento por condición (CBM-03, CBM-05): alertas y reglas.

Vistas delgadas: validan el formulario y llaman a los servicios; las lecturas pasan por los
selectores (permisos y alcance por sitio). El módulo `condition` lo exige el middleware de
módulos para toda ruta bajo /condition/.
"""

import uuid

from django.contrib import messages
from django.core.paginator import Paginator
from django.http import Http404
from django.shortcuts import redirect, render
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from apps.accounts.permissions import Perm, PermissionDeniedError
from apps.assets.models import AssetClass
from apps.billing.access import can_act
from apps.core.decorators import membership_required

from . import services
from .forms import AlertFilterForm, ConditionRuleForm, apply_errors
from .selectors import alert_for, alert_list, rule_for, rule_list, visible_rules

PAGE_SIZE = 25
RULE_STATUSES = ("inactive", "all")


# --- Alertas (CBM-05) --------------------------------------------------------------


@membership_required(perm=Perm.CONDITION_VIEW)
def alert_list_view(request):
    """Alertas con las abiertas primero; por defecto las activas (abiertas y reconocidas)."""
    membership = request.membership
    form = AlertFilterForm(request.GET or None, membership=membership)
    if form.is_bound:
        form.is_valid()  # los campos con error se ignoran y se muestran en el formulario
    alerts = alert_list(membership=membership, **form.filters())
    page = Paginator(alerts, PAGE_SIZE).get_page(request.GET.get("page"))
    active = form.active_count if form.is_bound else 0
    return render(
        request,
        "condition/alert_list.html",
        {
            "page": page,
            "filter_form": form,
            "active_filters": active,
            "filters_open": bool(active or form.errors),
            # Estado vacío útil: sin reglas, el llamado es crear la primera.
            "has_rules": page.paginator.count > 0 or visible_rules(membership=membership).exists(),
            "can_manage": can_act(membership, Perm.CONDITION_MANAGE),
        },
    )


def _get_alert(membership, pk):
    alert = alert_for(membership=membership, pk=pk)
    if alert is None:
        raise Http404
    return alert


@membership_required(perm=Perm.CONDITION_VIEW)
def alert_detail_view(request, pk):
    """Una alerta: valores, lectura que la abrió, transiciones y OT (enlace de los avisos)."""
    membership = request.membership
    alert = _get_alert(membership, pk)
    return render(
        request,
        "condition/alert_detail.html",
        {
            "alert": alert,
            "reading": alert.reading,
            "can_manage": can_act(membership, Perm.CONDITION_MANAGE),
        },
    )


@require_POST
@membership_required(perm=Perm.CONDITION_MANAGE)
def alert_acknowledge_view(request, pk):
    """Reconocer una alerta abierta. Con htmx devuelve la fila de la lista; sin htmx, al detalle."""
    membership = request.membership
    alert = _get_alert(membership, pk)
    htmx = bool(request.headers.get("HX-Request"))
    try:
        services.acknowledge_alert(membership=membership, alert=alert)
    except PermissionDeniedError:
        raise Http404 from None
    except services.ConditionError as error:
        messages.error(request, " ".join(error.messages))
    else:
        if not htmx:
            messages.success(request, _("Alerta reconocida."))
    if htmx:
        return render(
            request,
            "condition/_alert_row.html",
            {"alert": _get_alert(membership, pk), "can_manage": True},
        )
    return redirect("condition_alert_detail", pk=alert.pk)


# --- Reglas (CBM-03) ---------------------------------------------------------------


@membership_required(perm=Perm.CONDITION_VIEW)
def rule_list_view(request):
    """Reglas activas (por defecto), inactivas o todas; filtro por clase de activo."""
    membership = request.membership
    status = request.GET.get("status", "")
    if status not in RULE_STATUSES:
        status = ""
    # Opciones del filtro: solo las clases que tienen reglas.
    classes = list(AssetClass.objects.filter(condition_rules__isnull=False).distinct().order_by("name"))
    asset_class = next((cls for cls in classes if str(cls.pk) == request.GET.get("asset_class", "")), None)
    rules = rule_list(
        membership=membership,
        include_inactive=status == "all",
        inactive_only=status == "inactive",
        asset_class=asset_class,
    )
    page = Paginator(rules, PAGE_SIZE).get_page(request.GET.get("page"))
    return render(
        request,
        "condition/rule_list.html",
        {
            "page": page,
            "status": status,
            "classes": classes,
            "asset_class": asset_class,
            "filtered": bool(status or asset_class),
            "can_manage": can_act(membership, Perm.CONDITION_MANAGE),
        },
    )


@membership_required(perm=Perm.CONDITION_MANAGE)
def rule_create_view(request):
    membership = request.membership
    if request.method == "POST":
        form = ConditionRuleForm(request.POST, membership=membership)
        if form.is_valid():
            try:
                rule = services.create_rule(membership=membership, **form.service_kwargs())
            except services.ConditionError as error:
                apply_errors(form, error)
            else:
                messages.success(request, _("Regla creada: %(rule)s.") % {"rule": rule.label})
                return redirect("condition_rule_list")
    else:
        form = ConditionRuleForm(membership=membership)
        # Desde la ficha del activo: la variable viene elegida (solo si es una de las opciones).
        meter = _safe_uuid(request.GET.get("meter"))
        if meter and form.fields["meter"].queryset.filter(pk=meter).exists():
            form = ConditionRuleForm(membership=membership, initial={"meter": meter})
    return render(request, "condition/rule_form.html", {"form": form, "rule": None})


def _safe_uuid(value):
    try:
        return uuid.UUID(value) if value else None
    except ValueError:
        return None


def _get_rule(membership, pk):
    rule = rule_for(membership=membership, pk=pk)
    if rule is None:
        raise Http404
    return rule


@membership_required(perm=Perm.CONDITION_MANAGE)
def rule_edit_view(request, pk):
    """Cambia la condición, la acción y los datos de la OT; el alcance no se edita."""
    membership = request.membership
    rule = _get_rule(membership, pk)
    form = ConditionRuleForm.for_rule(rule, membership=membership, data=request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            services.update_rule(membership=membership, rule=rule, **form.service_kwargs())
        except PermissionDeniedError:
            raise Http404 from None
        except services.ConditionError as error:
            apply_errors(form, error)
        else:
            messages.success(request, _("Cambios guardados."))
            return redirect("condition_rule_list")
    return render(request, "condition/rule_form.html", {"form": form, "rule": rule})


@require_POST
@membership_required(perm=Perm.CONDITION_MANAGE)
def rule_active_view(request, pk):
    """Activa o desactiva una regla (sin borrado físico)."""
    membership = request.membership
    rule = _get_rule(membership, pk)
    is_active = request.POST.get("is_active") == "1"
    try:
        services.set_rule_active(membership=membership, rule=rule, is_active=is_active)
    except PermissionDeniedError:
        raise Http404 from None
    if is_active:
        messages.success(request, _("Regla activada: evalúa desde la próxima lectura."))
    else:
        messages.success(request, _("Regla desactivada: sus alertas activas quedaron normalizadas."))
    return redirect("condition_rule_list")
