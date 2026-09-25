from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from django.views.decorators.http import require_POST

from apps.accounts.models import Membership
from apps.accounts.permissions import Perm, PermissionDeniedError, has_perm
from apps.attachments.models import TargetType
from apps.attachments.selectors import attachments_for
from apps.core.decorators import membership_required

from .forms import (
    CompleteWorkOrderForm,
    FailureCodeForm,
    FailureCodeRenameForm,
    LaborForm,
    OtherCostForm,
    PartUsageForm,
    WorkOrderCreateForm,
    WorkOrderFilterForm,
)
from .models import FailureCode, OtherCost, PartUsage, WorkOrder, WorkOrderChecklistItem
from .selectors import (
    failure_code_list,
    open_requests_for_asset,
    pending_requests,
    visible_requests,
    visible_work_orders,
    work_order_detail,
    work_order_list,
)
from .services import (
    COST_LOCKED_STATUSES,
    WorkOrderError,
    accept_request,
    add_other_cost,
    add_part_usage,
    allowed_transitions,
    answer_checklist_item,
    complete_work_order,
    create_failure_code,
    create_work_order,
    log_labor,
    merge_request,
    reject_request,
    remove_cost_line,
    rename_failure_code,
    set_failure_code_active,
    transition,
    work_order_meter,
)

SCOPES = [("open", _("Abiertas")), ("overdue", _("Vencidas")), ("mine", _("Mis OT")), ("done", _("Cerradas"))]


def _query_with(request, **overrides):
    """Querystring actual (sin `page`) con las claves de `overrides` reemplazadas o quitadas."""
    data = request.GET.copy()
    data.pop("page", None)
    for key, value in overrides.items():
        if value:
            data[key] = value
        else:
            data.pop(key, None)
    return data.urlencode()


@membership_required
def work_order_list_view(request):
    membership = request.membership
    default_scope = "mine" if membership.role == Membership.Role.TECHNICIAN else "open"
    scope = request.GET.get("scope", default_scope)
    work_type = request.GET.get("type", "")

    filter_form = WorkOrderFilterForm(request.GET, membership=membership)
    filter_form.is_valid()
    filters = filter_form.cleaned_data

    assigned_to = request.user if scope == "mine" else filters.get("assigned_to")
    qs = work_order_list(
        membership=membership,
        scope="open" if scope == "mine" else scope,
        work_type=work_type,
        status=filters.get("status", ""),
        assigned_to=assigned_to,
        asset_q=filters.get("asset_q", ""),
        location=filters.get("location"),
        date_field=filters.get("date_field") or "due_on",
        date_from=filters.get("date_from"),
        date_to=filters.get("date_to"),
    )
    page = Paginator(qs, 25).get_page(request.GET.get("page"))
    return render(
        request,
        "work/list.html",
        {
            "page": page,
            "scope": scope,
            "scope_links": [(value, label, _query_with(request, scope=value)) for value, label in SCOPES],
            "work_type": work_type,
            "type_links": [("", _("Todos los tipos"), _query_with(request, type=""))]
            + [(value, label, _query_with(request, type=value)) for value, label in WorkOrder.Type.choices],
            "filter_form": filter_form,
        },
    )


@membership_required(perm=Perm.WORK_ORDER_MANAGE)
def work_order_create_view(request):
    membership = request.membership
    form = WorkOrderCreateForm(request.POST or None, membership=membership)
    if request.method == "POST" and form.is_valid():
        work_order = create_work_order(by=request.user, **form.cleaned_data)
        messages.success(request, _("%(code)s creada.") % {"code": work_order.code})
        return redirect("work_order_detail", pk=work_order.pk)
    return render(request, "work/create.html", {"form": form})


def _complete_form(work_order, data=None):
    meter = work_order_meter(work_order)
    plan = work_order.plan_assignment.plan if work_order.plan_assignment_id else None
    return CompleteWorkOrderForm(
        data,
        with_failure=work_order.work_type == WorkOrder.Type.CORRECTIVE,
        meter=meter,
        meter_required=bool(plan and plan.uses_meter and meter),
        require_failure_details=work_order.organization.require_failure_details,
        asset=work_order.asset,
    )


def _detail_context(request, work_order, complete_form=None):
    targets = allowed_transitions(work_order=work_order, membership=request.membership)
    return {
        "wo": work_order,
        "transitions": [(s, WorkOrder.Status(s).label) for s in targets if s != WorkOrder.Status.COMPLETED],
        "can_execute": has_perm(request.membership, Perm.WORK_ORDER_EXECUTE) and work_order.is_open,
        "complete_form": complete_form or _complete_form(work_order),
        "pending_required": sum(1 for i in work_order.checklist.all() if i.required and not i.result),
        "costs_editable": work_order.status not in COST_LOCKED_STATUSES,
        "can_manage": has_perm(request.membership, Perm.WORK_ORDER_MANAGE),
        "labor_form": LaborForm(),
        "part_form": PartUsageForm(),
        "other_cost_form": OtherCostForm(),
        "attachments": attachments_for(target_type=TargetType.WORK_ORDER, target_id=work_order.pk),
    }


def _get_detail_or_404(request, pk):
    try:
        return work_order_detail(pk, membership=request.membership)
    except WorkOrder.DoesNotExist as exc:
        raise Http404 from exc


@membership_required
def work_order_detail_view(request, pk):
    work_order = _get_detail_or_404(request, pk)
    return render(request, "work/detail.html", _detail_context(request, work_order))


@require_POST
@membership_required
def work_order_transition_view(request, pk):
    work_order = get_object_or_404(visible_work_orders(membership=request.membership), pk=pk)
    try:
        transition(
            work_order=work_order,
            to_status=request.POST.get("to", ""),
            membership=request.membership,
            comment=request.POST.get("comment", "")[:500],
        )
    except WorkOrderError as exc:
        messages.error(request, exc.message)
    else:
        messages.success(request, f"{work_order.code}: {work_order.get_status_display()}.")
    return redirect("work_order_detail", pk=pk)


@require_POST
@membership_required(perm=Perm.WORK_ORDER_EXECUTE)
def checklist_answer_view(request, pk, item_pk):
    item = get_object_or_404(
        WorkOrderChecklistItem.objects.select_related("work_order").filter(
            work_order__in=visible_work_orders(membership=request.membership)
        ),
        pk=item_pk,
        work_order_id=pk,
    )
    error = None
    try:
        answer_checklist_item(
            item=item,
            membership=request.membership,
            result=request.POST.get("result", ""),
            value_number=request.POST.get("value_number") or None,
            note=request.POST.get("note", ""),
        )
    except WorkOrderError as exc:
        error = exc.message
    except PermissionDeniedError as exc:
        raise PermissionDenied from exc
    if request.headers.get("HX-Request"):
        response = render(
            request, "work/_checklist_item.html", {"item": item, "error": error, "can_execute": True}
        )
        # La cabecera de la OT puede cambiar (pasa a "En ejecución").
        response["HX-Trigger"] = "checklist-changed"
        return response
    if error:
        messages.error(request, error)
    return redirect("work_order_detail", pk=pk)


@require_POST
@membership_required(perm=Perm.WORK_ORDER_EXECUTE)
def work_order_complete_view(request, pk):
    work_order = _get_detail_or_404(request, pk)
    form = _complete_form(work_order, request.POST)
    if form.is_valid():
        try:
            complete_work_order(
                work_order=work_order,
                membership=request.membership,
                labor_minutes=form.cleaned_data["labor_minutes"],
                notes=form.cleaned_data["notes"],
                failure_mode=form.cleaned_data.get("failure_mode"),
                failure_cause=form.cleaned_data.get("failure_cause"),
                failure_mechanism=form.cleaned_data.get("failure_mechanism"),
                detection_method=form.cleaned_data.get("detection_method"),
                component=form.cleaned_data.get("component"),
                meter_value=form.cleaned_data.get("meter_value"),
            )
        except ValidationError as exc:
            messages.error(request, " ".join(exc.messages))
        else:
            messages.success(request, f"{work_order.code} completada.")
            return redirect("work_order_detail", pk=pk)
    work_order = work_order_detail(pk, membership=request.membership)
    return render(request, "work/detail.html", _detail_context(request, work_order, form), status=400)


# --- Costos (WO-04) -----------------------------------------------------------


def _costs_redirect(pk):
    return redirect(f"{reverse('work_order_detail', args=[pk])}#costs")


def _run_cost_action(request, pk, action):
    try:
        action()
    except PermissionDeniedError as exc:
        raise PermissionDenied from exc
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
    return _costs_redirect(pk)


@require_POST
@membership_required(perm=Perm.WORK_ORDER_EXECUTE)
def labor_add_view(request, pk):
    work_order = get_object_or_404(visible_work_orders(membership=request.membership), pk=pk)
    form = LaborForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Indica los minutos trabajados (1 a 1.440).")
        return _costs_redirect(pk)
    return _run_cost_action(
        request,
        pk,
        lambda: log_labor(
            work_order=work_order, membership=request.membership, minutes=form.cleaned_data["minutes"]
        ),
    )


@require_POST
@membership_required(perm=Perm.WORK_ORDER_EXECUTE)
def part_add_view(request, pk):
    work_order = get_object_or_404(visible_work_orders(membership=request.membership), pk=pk)
    form = PartUsageForm(request.POST)
    if not form.is_valid():
        messages.error(
            request, "Revisa el repuesto: descripción, cantidad y costo unitario son obligatorios."
        )
        return _costs_redirect(pk)
    return _run_cost_action(
        request,
        pk,
        lambda: add_part_usage(work_order=work_order, membership=request.membership, **form.cleaned_data),
    )


@require_POST
@membership_required(perm=Perm.WORK_ORDER_MANAGE)
def other_cost_add_view(request, pk):
    work_order = get_object_or_404(visible_work_orders(membership=request.membership), pk=pk)
    form = OtherCostForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Revisa el costo: descripción y monto son obligatorios.")
        return _costs_redirect(pk)
    return _run_cost_action(
        request,
        pk,
        lambda: add_other_cost(work_order=work_order, membership=request.membership, **form.cleaned_data),
    )


@require_POST
@membership_required(perm=Perm.WORK_ORDER_EXECUTE)
def cost_line_delete_view(request, pk, kind, line_pk):
    model = {"part": PartUsage, "other": OtherCost}.get(kind)
    if model is None:
        raise Http404
    visible = visible_work_orders(membership=request.membership)
    line = get_object_or_404(
        model.objects.select_related("work_order"), pk=line_pk, work_order__in=visible, work_order_id=pk
    )
    return _run_cost_action(request, pk, lambda: remove_cost_line(line=line, membership=request.membership))


# --- Bandeja de solicitudes (REQ-01 CA3) ---------------------------------------


@membership_required(perm=Perm.WORK_ORDER_MANAGE)
def request_list_view(request):
    page = Paginator(pending_requests(membership=request.membership), 25).get_page(request.GET.get("page"))
    return render(request, "work/request_list.html", {"page": page})


def _get_request_or_404(request, pk):
    return get_object_or_404(visible_requests(membership=request.membership), pk=pk)


@require_POST
@membership_required(perm=Perm.WORK_ORDER_MANAGE)
def request_accept_view(request, pk):
    work_request = _get_request_or_404(request, pk)
    try:
        accept_request(work_request=work_request, membership=request.membership)
    except WorkOrderError as exc:
        messages.error(request, exc.message)
    else:
        messages.success(request, f"Solicitud #{work_request.number} aceptada.")
    return redirect("request_list")


@require_POST
@membership_required(perm=Perm.WORK_ORDER_MANAGE)
def request_reject_view(request, pk):
    work_request = _get_request_or_404(request, pk)
    try:
        reject_request(
            work_request=work_request, membership=request.membership, reason=request.POST.get("reason", "")
        )
    except WorkOrderError as exc:
        messages.error(request, exc.message)
    else:
        messages.success(request, f"Solicitud #{work_request.number} rechazada.")
    return redirect("request_list")


@membership_required(perm=Perm.WORK_ORDER_MANAGE)
def request_merge_view(request, pk):
    work_request = _get_request_or_404(request, pk)
    candidates = open_requests_for_asset(asset=work_request.asset, exclude=work_request)
    if request.method == "POST":
        into = get_object_or_404(candidates, pk=request.POST.get("into", ""))
        try:
            merge_request(work_request=work_request, into=into, membership=request.membership)
        except WorkOrderError as exc:
            messages.error(request, exc.message)
        else:
            messages.success(request, f"Solicitud #{work_request.number} fusionada con #{into.number}.")
            return redirect("request_list")
    return render(
        request, "work/request_merge.html", {"work_request": work_request, "candidates": candidates}
    )


# --- Catálogo de falla (REQ-03 CA1) --------------------------------------------


@membership_required(perm=Perm.WORK_ORDER_MANAGE)
def failure_code_list_view(request):
    kind = request.GET.get("kind", "")
    page = Paginator(failure_code_list(kind=kind), 25).get_page(request.GET.get("page"))
    form = FailureCodeForm(initial={"kind": kind} if kind else None)
    return render(
        request,
        "work/failure_codes.html",
        {"page": page, "kind": kind, "kinds": FailureCode.Kind.choices, "form": form},
    )


@require_POST
@membership_required(perm=Perm.WORK_ORDER_MANAGE)
def failure_code_create_view(request):
    form = FailureCodeForm(request.POST)
    if form.is_valid():
        try:
            create_failure_code(membership=request.membership, **form.cleaned_data)
        except WorkOrderError as exc:
            messages.error(request, exc.message)
        else:
            messages.success(request, "Código agregado.")
    else:
        messages.error(request, "Revisa el tipo, código y nombre.")
    return redirect("failure_code_list")


@require_POST
@membership_required(perm=Perm.WORK_ORDER_MANAGE)
def failure_code_rename_view(request, pk):
    failure_code = get_object_or_404(FailureCode, pk=pk)
    form = FailureCodeRenameForm(request.POST)
    if form.is_valid():
        try:
            rename_failure_code(
                failure_code=failure_code, name=form.cleaned_data["name"], membership=request.membership
            )
        except WorkOrderError as exc:
            messages.error(request, exc.message)
        else:
            messages.success(request, f"{failure_code.code} actualizado.")
    else:
        messages.error(request, "El nombre es obligatorio.")
    return redirect("failure_code_list")


@require_POST
@membership_required(perm=Perm.WORK_ORDER_MANAGE)
def failure_code_toggle_view(request, pk):
    failure_code = get_object_or_404(FailureCode, pk=pk)
    set_failure_code_active(
        failure_code=failure_code, is_active=not failure_code.is_active, membership=request.membership
    )
    return redirect("failure_code_list")
