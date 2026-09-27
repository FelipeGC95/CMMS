"""Pantallas del asistente de puesta en marcha (ORG-03). Solo administradores de la organización."""

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.http import Http404
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.translation import gettext as _
from django.utils.translation import ngettext
from django.views.decorators.http import require_POST

from apps.accounts.permissions import Perm
from apps.assets.models import Asset, AssetClass
from apps.core.decorators import membership_required
from apps.organizations.models import Location
from apps.organizations.modules import has_module, plan_message

from . import services
from .forms import AssetRowFormSet, IndustryForm, LocationsForm, PlansForm, ReadingsForm
from .models import STEPS, Step

admin_required = membership_required(perm=Perm.ORGANIZATION_MANAGE)
PREVENTIVE_STEPS = {Step.PLANS, Step.READINGS, Step.CALENDAR}


def _step_url(step):
    return reverse("onboarding_step", args=[step])


def _next(state):
    if state.is_finished:
        return redirect("dashboard")
    return redirect(_step_url(state.current_step))


def _render(request, state, step, template, context):
    statuses = []
    for number, (value, label) in enumerate(Step.choices, start=1):
        if value in state.completed_steps:
            status = "done"
        elif value in state.skipped_steps:
            status = "skipped"
        else:
            status = "pending"
        statuses.append(
            {"value": value, "label": label, "number": number, "status": status, "active": value == step}
        )
    blocked = step in PREVENTIVE_STEPS and not has_module(request.organization, "preventive")
    return render(
        request,
        template,
        {
            "state": state,
            "step": step,
            "step_number": STEPS.index(step) + 1,
            "step_total": len(STEPS),
            "steps": statuses,
            "blocked_message": plan_message(request.organization, "preventive") if blocked else "",
            **context,
        },
    )


@admin_required
def onboarding_view(request):
    """Entrada al asistente: lleva al paso en que quedó (o al primero)."""
    state = services.open_wizard(membership=request.membership)
    return redirect(_step_url(STEPS[0] if state.is_finished else state.current_step))


@admin_required
def step_view(request, step):
    handler = STEP_VIEWS.get(step)
    if handler is None:
        raise Http404
    state = services.open_wizard(membership=request.membership)
    return handler(request, state)


def _industry(request, state):
    form = IndustryForm(request.POST or None, initial={"industry": state.industry or None})
    if request.method == "POST" and form.is_valid():
        summary = services.choose_industry(
            membership=request.membership, industry=form.cleaned_data["industry"]
        )
        messages.success(
            request,
            _("Rubro «%(name)s»: %(classes)s clases de activo y %(codes)s códigos de falla copiados.")
            % {"name": summary.name, "classes": len(summary.classes), "codes": len(summary.failure_codes)},
        )
        return redirect(_step_url(Step.LOCATIONS))
    return _render(request, state, Step.INDUSTRY, "onboarding/industry.html", {"form": form})


def _locations(request, state):
    form = LocationsForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            created = services.create_locations(
                membership=request.membership,
                site_name=form.cleaned_data["site_name"],
                areas=form.cleaned_data["areas"],
            )
        except ValidationError as error:
            form.add_error(None, error)
        else:
            messages.success(
                request,
                ngettext("%(n)s ubicación lista.", "%(n)s ubicaciones listas.", len(created))
                % {"n": len(created)},
            )
            return redirect(_step_url(Step.ASSETS))
    locations = Location.objects.filter(is_active=True).order_by("path")[:50]
    return _render(
        request, state, Step.LOCATIONS, "onboarding/locations.html", {"form": form, "locations": locations}
    )


def _assets(request, state):
    classes = AssetClass.objects.filter(is_active=True).order_by("name")
    locations = Location.objects.filter(is_active=True).order_by("path")
    formset = AssetRowFormSet(
        request.POST or None, prefix="rows", form_kwargs={"classes": classes, "locations": locations}
    )
    if request.method == "POST" and formset.is_valid():
        rows = [form.cleaned_data for form in formset if form.cleaned_data.get("name")]
        try:
            created, skipped = services.quick_add_assets(membership=request.membership, rows=rows)
        except ValidationError as error:
            formset.forms[0].add_error(None, error)
        else:
            if created:
                messages.success(
                    request,
                    ngettext("%(n)s activo creado.", "%(n)s activos creados.", len(created))
                    % {"n": len(created)},
                )
            if skipped:
                messages.info(
                    request, _("Ya existían y se omitieron: %(names)s.") % {"names": ", ".join(skipped)}
                )
            if request.POST.get("action") == "more":
                return redirect(_step_url(Step.ASSETS))
            services.finish_assets(membership=request.membership)
            return redirect(_step_url(Step.PLANS))
    assets = Asset.objects.filter(is_active=True).select_related("asset_class", "location").order_by("code")
    return _render(
        request,
        state,
        Step.ASSETS,
        "onboarding/assets.html",
        {
            "formset": formset,
            "assets": assets[:20],
            "asset_total": assets.count(),
            "has_classes": classes.exists(),
        },
    )


def _plans(request, state):
    plans = services.suggested_plans(membership=request.membership)
    form = PlansForm(request.POST or None, plans=plans)
    if request.method == "POST" and form.is_valid():
        created, assigned, warnings = services.create_suggested_plans(
            membership=request.membership, plan_codes=form.cleaned_data["plan_codes"]
        )
        messages.success(
            request,
            _("%(plans)s planes creados y %(assigned)s asignaciones a activos.")
            % {"plans": created, "assigned": assigned},
        )
        for warning in warnings[:10]:
            messages.warning(request, warning)
        return redirect(_step_url(Step.READINGS))
    return _render(request, state, Step.PLANS, "onboarding/plans.html", {"form": form, "plans": plans})


def _readings(request, state):
    form = ReadingsForm(request.POST or None, meters=services.meters_for_readings())
    if request.method == "POST" and form.is_valid():
        try:
            recorded = services.record_initial_readings(membership=request.membership, values=form.values())
        except ValidationError as error:
            form.add_error(None, error)
        else:
            if recorded:
                messages.success(
                    request,
                    ngettext("%(n)s lectura registrada.", "%(n)s lecturas registradas.", recorded)
                    % {"n": recorded},
                )
            return redirect(_step_url(Step.CALENDAR))
    return _render(request, state, Step.READINGS, "onboarding/readings.html", {"form": form})


def _calendar(request, state):
    if request.method == "POST":
        services.finish(membership=request.membership)
        messages.success(request, _("¡Listo! Tu mantenimiento preventivo ya está en marcha."))
        return redirect("dashboard")
    summary = None
    if has_module(request.organization, "preventive"):
        summary = services.calendar_summary(membership=request.membership)
    return _render(request, state, Step.CALENDAR, "onboarding/calendar.html", {"summary": summary})


STEP_VIEWS = {
    Step.INDUSTRY: _industry,
    Step.LOCATIONS: _locations,
    Step.ASSETS: _assets,
    Step.PLANS: _plans,
    Step.READINGS: _readings,
    Step.CALENDAR: _calendar,
}


@require_POST
@admin_required
def skip_view(request, step):
    if step not in STEPS:
        raise Http404
    state = services.skip_step(membership=request.membership, step=step)
    return _next(state)


@require_POST
@admin_required
def dismiss_view(request):
    services.dismiss(membership=request.membership)
    messages.info(request, _("Puedes retomar la puesta en marcha cuando quieras desde Configuración."))
    return redirect("dashboard")
