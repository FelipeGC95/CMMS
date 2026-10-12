"""Pantallas de planes preventivos (PM-01..PM-04): lista, ficha, borrador y asignaciones.

Todas exigen `plan.manage` (planificador y administrador). Las vistas validan
la entrada y llaman a servicios; el alcance por sitio se aplica al elegir activos.
"""

import uuid

from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.translation import gettext as _
from django.utils.translation import ngettext
from django.views.decorators.http import require_POST

from apps.accounts.permissions import Perm, PermissionDeniedError
from apps.assets.models import Meter
from apps.assets.selectors import visible_assets
from apps.core.decorators import membership_required
from apps.organizations.modules import has_module
from apps.organizations.selectors import descendant_ids

from . import services
from .forms import (
    AssetPlanForm,
    AssignFilterForm,
    MaintenanceSettingsForm,
    PartLinkForm,
    PlanFilterForm,
    PlanForm,
    PlanItemForm,
    PlanResourceForm,
    PublishForm,
    TriggerForm,
)
from .models import MaintenancePlan, PlanAbsorption, PlanAssignment, PlanResource, PlanTaskItem, PlanVersion
from .selectors import (
    assignable_assets,
    assignable_plans,
    maintenance_settings,
    plan_absorptions,
    plan_assignments,
    plan_list,
    plan_versions,
    version_content,
)

manager_required = membership_required(perm=Perm.PLAN_MANAGE)


def _plan(pk):
    return get_object_or_404(MaintenancePlan.objects.select_related("asset_class"), pk=pk)


def _draft_or_none(plan):
    return plan.versions.filter(status=PlanVersion.Status.DRAFT).first()


def _draft(plan):
    draft = _draft_or_none(plan)
    if draft is None:
        raise Http404  # sin borrador no hay nada que editar
    return draft


def _inventory_on(request) -> bool:
    """¿El plan de la organización incluye inventario? Sin él no hay buscador de catálogo (INV-11)."""
    return has_module(request.organization, "inventory")


def _selected_part(part_id):
    """Repuesto elegido en el buscador, para volver a mostrarlo marcado (o None)."""
    from apps.inventory.models import Part

    try:
        pk = uuid.UUID(str(part_id)) if part_id else None
    except ValueError:
        return None
    return Part.objects.filter(pk=pk).first() if pk else None


def _report_assignment(request, assigned, warnings):
    if assigned:
        messages.success(
            request,
            ngettext("Plan asignado a %(n)s activo.", "Plan asignado a %(n)s activos.", assigned)
            % {"n": assigned},
        )
    elif not warnings:
        messages.info(request, _("No había activos nuevos para asignar."))
    for warning in warnings:
        messages.warning(request, warning)


# --- Lista y ficha ----------------------------------------------------------


@manager_required
def plan_list_view(request):
    form = PlanFilterForm(request.GET or None)
    filters = form.cleaned_data if form.is_valid() else {}
    plans = plan_list(
        membership=request.membership,
        search=filters.get("q", "").strip(),
        active=filters.get("active", ""),
        asset_class=filters.get("asset_class"),
        trigger=filters.get("trigger", ""),
    )
    page = Paginator(plans, 25).get_page(request.GET.get("page"))
    active_filters = sum(1 for key in ("q", "active", "asset_class", "trigger") if filters.get(key))
    return render(
        request,
        "maintenance/plan_list.html",
        {"page": page, "filter_form": form, "active_filters": active_filters},
    )


@manager_required
def plan_create_view(request):
    plan_form = PlanForm(request.POST or None, show_active=False)
    trigger_form = TriggerForm(request.POST or None)
    if request.method == "POST" and plan_form.is_valid() and trigger_form.is_valid():
        try:
            plan = services.create_plan_with_draft(
                membership=request.membership,
                code=plan_form.cleaned_data["code"],
                name=plan_form.cleaned_data["name"],
                asset_class=plan_form.cleaned_data["asset_class"],
                **trigger_form.version_fields(),
            )
        except ValidationError as error:
            plan_form.add_error(None, error)
        else:
            messages.success(request, _("Plan creado. Agrega su checklist y publícalo."))
            return redirect("plan_draft", pk=plan.pk)
    return render(
        request, "maintenance/plan_create.html", {"plan_form": plan_form, "trigger_form": trigger_form}
    )


@manager_required
def plan_detail_view(request, pk):
    plan = _plan(pk)
    items, resources = version_content(plan.current_version)
    assignments = plan_assignments(membership=request.membership, plan=plan)
    page = Paginator(assignments, 25).get_page(request.GET.get("page"))
    return render(
        request,
        "maintenance/plan_detail.html",
        {
            "plan": plan,
            "version": plan.current_version,
            "draft": _draft_or_none(plan),
            "versions": plan_versions(plan=plan),
            "items": items,
            "resources": resources,
            "page": page,
            "link_parts": _inventory_on(request),
            **_absorption_context(plan),
        },
    )


# --- Borrador ---------------------------------------------------------------


@require_POST
@manager_required
def plan_draft_start_view(request, pk):
    plan = _plan(pk)
    services.create_draft(membership=request.membership, plan=plan)
    return redirect("plan_draft", pk=plan.pk)


def _trigger_initial(version):
    return {field: getattr(version, field) for field in services.DRAFT_FIELDS}


@manager_required
def plan_draft_view(request, pk):
    plan = _plan(pk)
    draft = _draft_or_none(plan)
    if draft is None:
        messages.info(request, _("Este plan no tiene un borrador. Crea uno para cambiarlo."))
        return redirect("plan_detail", pk=plan.pk)
    action = request.POST.get("action") if request.method == "POST" else None
    plan_form = PlanForm(
        request.POST if action == "header" else None,
        initial={
            "code": plan.code,
            "name": plan.name,
            "asset_class": plan.asset_class_id,
            "is_active": plan.is_active,
        },
        prefix="plan",
    )
    trigger_form = TriggerForm(
        request.POST if action == "header" else None, initial=_trigger_initial(draft), prefix="trigger"
    )
    item_form = PlanItemForm(request.POST if action == "item" else None, prefix="item")
    resource_form = PlanResourceForm(request.POST if action == "resource" else None, prefix="resource")
    membership = request.membership
    try:
        if action == "header" and plan_form.is_valid() and trigger_form.is_valid():
            services.update_plan(membership=membership, plan=plan, **plan_form.cleaned_data)
            services.update_draft(membership=membership, version=draft, **trigger_form.version_fields())
            messages.success(request, _("Cambios guardados en el borrador."))
            return redirect("plan_draft", pk=plan.pk)
        if action == "item" and item_form.is_valid():
            services.add_draft_item(membership=membership, version=draft, **item_form.cleaned_data)
            messages.success(request, _("Ítem agregado."))
            return redirect(f"{request.path}#items")
        if action == "resource" and resource_form.is_valid():
            data = resource_form.cleaned_data
            if not _inventory_on(request):
                data["part"] = None
            services.add_draft_resource(membership=membership, version=draft, **data)
            messages.success(request, _("Repuesto o herramienta agregado."))
            return redirect(f"{request.path}#resources")
    except ValidationError as error:
        form = {"header": plan_form, "item": item_form, "resource": resource_form}[action]
        form.add_error(None, error)
    items, resources = version_content(draft)
    return render(
        request,
        "maintenance/plan_draft.html",
        {
            "plan": plan,
            "draft": draft,
            "items": items,
            "resources": resources,
            "plan_form": plan_form,
            "trigger_form": trigger_form,
            "item_form": item_form,
            "resource_form": resource_form,
            "parts_on": _inventory_on(request),
            "selected_part": _selected_part(resource_form["part"].value() if action == "resource" else None),
            **_absorption_context(plan),
        },
    )


def _draft_item(plan, item_pk):
    return get_object_or_404(PlanTaskItem.objects.select_related("version"), pk=item_pk, version=_draft(plan))


def _draft_resource(plan, resource_pk):
    return get_object_or_404(
        PlanResource.objects.select_related("version"), pk=resource_pk, version=_draft(plan)
    )


@manager_required
def plan_item_edit_view(request, pk, item_pk):
    plan = _plan(pk)
    item = _draft_item(plan, item_pk)
    form = PlanItemForm(
        request.POST or None,
        initial={
            f: getattr(item, f)
            for f in (
                "text",
                "item_type",
                "unit",
                "min_value",
                "max_value",
                "required",
                "meter_kind",
                "meter_name",
            )
        },
    )
    if request.method == "POST" and form.is_valid():
        try:
            services.update_draft_item(membership=request.membership, item=item, **form.cleaned_data)
        except ValidationError as error:
            form.add_error(None, error)
        else:
            messages.success(request, _("Ítem actualizado."))
            return redirect(f"{_draft_url(plan)}#items")
    return render(
        request,
        "maintenance/draft_entry_form.html",
        {"plan": plan, "form": form, "title": _("Editar ítem del checklist"), "anchor": "items"},
    )


@manager_required
def plan_resource_edit_view(request, pk, resource_pk):
    plan = _plan(pk)
    resource = _draft_resource(plan, resource_pk)
    form = PlanResourceForm(
        request.POST or None,
        initial={
            **{f: getattr(resource, f) for f in ("kind", "description", "quantity", "unit")},
            "part": resource.part_id,
        },
    )
    parts_on = _inventory_on(request)
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        if not parts_on:  # sin el módulo no se ve el buscador: el enlace queda como estaba
            data["part"] = resource.part_id
        try:
            services.update_draft_resource(membership=request.membership, resource=resource, **data)
        except ValidationError as error:
            form.add_error(None, error)
        else:
            messages.success(request, _("Repuesto o herramienta actualizado."))
            return redirect(f"{_draft_url(plan)}#resources")
    return render(
        request,
        "maintenance/draft_entry_form.html",
        {
            "plan": plan,
            "form": form,
            "title": _("Editar repuesto o herramienta"),
            "anchor": "resources",
            "parts_on": parts_on,
            "selected_part": _selected_part(form["part"].value()) if parts_on else None,
        },
    )


@manager_required
def plan_resource_link_view(request, pk, resource_pk):
    """Enlaza al catálogo un repuesto de la versión vigente (INV-11 CA2, excepción al ADR-016)."""
    plan = _plan(pk)
    if not _inventory_on(request) or plan.current_version_id is None:
        raise Http404
    resource = get_object_or_404(
        PlanResource.objects.select_related("version"),
        pk=resource_pk,
        version_id=plan.current_version_id,
        kind=PlanResource.Kind.PART,
        part__isnull=True,
    )
    form = PartLinkForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            services.link_published_resource(
                membership=request.membership, resource=resource, part=form.cleaned_data["part"]
            )
        except ValidationError as error:
            form.add_error(None, error)
        else:
            messages.success(request, _("Repuesto enlazado al catálogo."))
            return redirect(f"{reverse('plan_detail', args=[plan.pk])}#content-title")
    return render(
        request,
        "maintenance/plan_resource_link.html",
        {
            "plan": plan,
            "resource": resource,
            "form": form,
            "selected_part": _selected_part(form["part"].value() if request.method == "POST" else None),
        },
    )


def _draft_url(plan):
    return reverse("plan_draft", args=[plan.pk])


@require_POST
@manager_required
def plan_item_remove_view(request, pk, item_pk):
    plan = _plan(pk)
    services.remove_draft_item(membership=request.membership, item=_draft_item(plan, item_pk))
    messages.success(request, _("Ítem quitado del borrador."))
    return redirect(f"{_draft_url(plan)}#items")


@require_POST
@manager_required
def plan_item_move_view(request, pk, item_pk):
    plan = _plan(pk)
    services.move_draft_item(
        membership=request.membership,
        item=_draft_item(plan, item_pk),
        direction=request.POST.get("direction", ""),
    )
    return redirect(f"{_draft_url(plan)}#items")


@require_POST
@manager_required
def plan_resource_remove_view(request, pk, resource_pk):
    plan = _plan(pk)
    services.remove_draft_resource(membership=request.membership, resource=_draft_resource(plan, resource_pk))
    messages.success(request, _("Repuesto o herramienta quitado del borrador."))
    return redirect(f"{_draft_url(plan)}#resources")


@manager_required
def plan_publish_view(request, pk):
    """Confirmación de publicar: la versión queda inmutable y se recalculan los vencimientos."""
    plan = _plan(pk)
    draft = _draft_or_none(plan)
    if draft is None:
        return redirect("plan_detail", pk=plan.pk)
    form = PublishForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            services.publish_draft(
                membership=request.membership, version=draft, change_note=form.cleaned_data["change_note"]
            )
        except ValidationError as error:
            form.add_error(None, error)
        else:
            messages.success(request, _("Versión %(n)s publicada.") % {"n": draft.number})
            return redirect("plan_detail", pk=plan.pk)
    items, resources = version_content(draft)
    return render(
        request,
        "maintenance/plan_publish.html",
        {
            "plan": plan,
            "draft": draft,
            "form": form,
            "item_count": len(items),
            "resource_count": len(resources),
            "assignment_count": plan.assignments.filter(is_active=True).count(),
        },
    )


@manager_required
def plan_discard_view(request, pk):
    plan = _plan(pk)
    draft = _draft_or_none(plan)
    if draft is None:
        return redirect("plan_detail", pk=plan.pk)
    if request.method == "POST":
        services.discard_draft(membership=request.membership, version=draft)
        messages.success(request, _("Borrador descartado."))
        return redirect("plan_detail", pk=plan.pk)
    return render(request, "maintenance/plan_discard.html", {"plan": plan, "draft": draft})


# --- Asignaciones -----------------------------------------------------------


@manager_required
def plan_assign_view(request, pk):
    """Selector de activos (PM-04 CA1): filtra por clase y ubicación dentro del alcance."""
    plan = _plan(pk)
    if plan.current_version is None or not plan.is_active:
        messages.info(request, _("Solo se asignan planes activos con una versión publicada."))
        return redirect("plan_detail", pk=plan.pk)
    membership = request.membership
    if request.method == "POST":
        ids = request.POST.getlist("assets")
        try:
            assets = list(visible_assets(membership=membership).filter(pk__in=ids))
        except ValidationError:
            assets = []
        meters = {}
        if plan.uses_meter:
            wanted = [request.POST.get(f"meter-{a.pk}") for a in assets]
            try:
                chosen = Meter.objects.filter(pk__in=[m for m in wanted if m], asset__in=assets)
                meters = {m.asset_id: m for m in chosen}
            except ValidationError:
                meters = {}
        if not assets:
            messages.error(request, _("Marca al menos un activo."))
        else:
            try:
                assigned, warnings = services.assign_plan_to_assets(
                    membership=membership, plan=plan, targets=[(a, meters.get(a.pk)) for a in assets]
                )
            except ValidationError as error:
                messages.error(request, " ".join(error.messages))
            else:
                _report_assignment(request, assigned, warnings)
                return redirect("plan_detail", pk=plan.pk)
    filter_form = AssignFilterForm(request.GET or None, membership=membership)
    filters = filter_form.cleaned_data if filter_form.is_valid() else {}
    location = filters.get("location")
    assets = assignable_assets(
        membership=membership,
        plan=plan,
        asset_class=filters.get("asset_class") or (None if request.GET else plan.asset_class),
        location_ids={location.pk, *descendant_ids(location)} if location else None,
        search=filters.get("q", "").strip(),
    )
    if not request.GET and plan.asset_class_id:
        filter_form = AssignFilterForm(initial={"asset_class": plan.asset_class_id}, membership=membership)
    page = Paginator(assets, 50).get_page(request.GET.get("page"))
    return render(
        request,
        "maintenance/plan_assign.html",
        {"plan": plan, "page": page, "filter_form": filter_form},
    )


@require_POST
@manager_required
def plan_assign_class_view(request, pk):
    plan = _plan(pk)
    try:
        assigned, warnings = services.assign_plan_to_class(membership=request.membership, plan=plan)
    except ValidationError as error:
        messages.error(request, " ".join(error.messages))
    else:
        _report_assignment(request, assigned, warnings)
    return redirect("plan_detail", pk=plan.pk)


@require_POST
@manager_required
def assignment_deactivate_view(request, pk, assignment_pk):
    plan = _plan(pk)
    assignment = get_object_or_404(
        PlanAssignment.objects.filter(asset__in=visible_assets(membership=request.membership)),
        pk=assignment_pk,
        plan=plan,
    )
    services.deactivate_assignment(membership=request.membership, assignment=assignment)
    messages.success(request, _("Plan quitado de %(code)s.") % {"code": assignment.asset.code})
    return redirect("plan_detail", pk=plan.pk)


@manager_required
def asset_plan_assign_view(request, pk):
    """Asignar un plan desde la ficha del activo (PM-04 CA1, individual)."""
    asset = get_object_or_404(visible_assets(membership=request.membership), pk=pk, is_active=True)
    plans = assignable_plans(asset=asset)
    meters = asset.meters.filter(is_active=True).order_by("kind", "name")
    form = AssetPlanForm(request.POST or None, plans=plans, meters=meters)
    if request.method == "POST" and form.is_valid():
        plan, meter = form.cleaned_data["plan"], form.cleaned_data.get("meter")
        if meter is not None and (not plan.uses_meter or meter.kind != plan.meter_kind):
            meter = None
        try:
            assigned, warnings = services.assign_plan_to_assets(
                membership=request.membership, plan=plan, targets=[(asset, meter)]
            )
        except PermissionDeniedError as exc:
            raise PermissionDenied from exc
        except ValidationError as error:
            form.add_error(None, error)
        else:
            if warnings:
                for warning in warnings:
                    form.add_error(None, warning)
            else:
                _report_assignment(request, assigned, warnings)
                return redirect("asset_detail", pk=asset.pk)
    return render(
        request, "maintenance/asset_plan_assign.html", {"asset": asset, "form": form, "plans": plans}
    )


# --- Ajustes del programador (PM-05 CA1) -----------------------------------------


@membership_required(perm=Perm.ORGANIZATION_MANAGE)
def maintenance_settings_view(request):
    """Horizonte de generación de OT preventivas de la organización (solo administradores)."""
    current = maintenance_settings()
    form = MaintenanceSettingsForm(request.POST or None, initial={"horizon_days": current.horizon_days})
    if request.method == "POST" and form.is_valid():
        try:
            services.update_maintenance_settings(
                membership=request.membership, horizon_days=form.cleaned_data["horizon_days"]
            )
        except ValidationError as error:
            form.add_error(None, error)
        else:
            messages.success(request, _("Ajustes guardados."))
            return redirect("maintenance_settings")
    return render(request, "maintenance/settings.html", {"form": form})


# --- Absorción de planes (PM-05 CA3) --------------------------------------------


def _absorption_context(plan):
    absorptions, choices = plan_absorptions(plan=plan)
    return {"absorptions": absorptions, "absorb_choices": choices}


def _back(request, plan):
    """Vuelve a la ficha o al borrador (solo rutas del propio plan)."""
    target = request.POST.get("next", "")
    allowed = {reverse("plan_detail", args=[plan.pk]), reverse("plan_draft", args=[plan.pk])}
    return redirect(f"{target if target in allowed else allowed.pop()}#absorptions")


@require_POST
@manager_required
def plan_absorption_add_view(request, pk):
    plan = _plan(pk)
    # El manager por defecto filtra por organización: un plan de otra empresa da 404.
    absorbed = get_object_or_404(MaintenancePlan, pk=request.POST.get("absorbed_plan") or None)
    try:
        services.add_plan_absorption(membership=request.membership, plan=plan, absorbed_plan=absorbed)
    except ValidationError as error:
        messages.error(request, " ".join(error.messages))
    else:
        messages.success(
            request, _("«%(plan)s» absorberá a «%(other)s».") % {"plan": plan.name, "other": absorbed.name}
        )
    return _back(request, plan)


@require_POST
@manager_required
def plan_absorption_remove_view(request, pk, absorption_pk):
    plan = _plan(pk)
    absorption = get_object_or_404(
        PlanAbsorption.objects.select_related("plan", "absorbed_plan"), pk=absorption_pk, plan=plan
    )
    services.remove_plan_absorption(membership=request.membership, absorption=absorption)
    messages.success(request, _("Absorción quitada."))
    return _back(request, plan)
