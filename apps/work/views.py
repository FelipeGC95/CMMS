from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from apps.accounts.permissions import Perm, PermissionDeniedError, has_perm
from apps.core.decorators import membership_required

from .forms import CompleteWorkOrderForm
from .models import Attachment, WorkOrder, WorkOrderChecklistItem
from .selectors import work_order_detail, work_order_list
from .services import (
    WorkOrderError,
    allowed_transitions,
    answer_checklist_item,
    complete_work_order,
    transition,
    work_order_meter,
)

SCOPES = [("open", "Abiertas"), ("overdue", "Vencidas"), ("mine", "Mis OT"), ("done", "Cerradas")]


@membership_required
def work_order_list_view(request):
    scope = request.GET.get("scope", "open")
    work_type = request.GET.get("type", "")
    if scope == "mine":
        qs = work_order_list(scope="open", work_type=work_type, assigned_to=request.user)
    else:
        qs = work_order_list(scope=scope, work_type=work_type)
    page = Paginator(qs, 25).get_page(request.GET.get("page"))
    return render(
        request,
        "work/list.html",
        {
            "page": page,
            "scope": scope,
            "scopes": SCOPES,
            "work_type": work_type,
            "types": WorkOrder.Type.choices,
        },
    )


def _complete_form(work_order, data=None):
    meter = work_order_meter(work_order)
    plan = work_order.plan_assignment.plan if work_order.plan_assignment_id else None
    return CompleteWorkOrderForm(
        data,
        with_failure=work_order.work_type == WorkOrder.Type.CORRECTIVE,
        meter=meter,
        meter_required=bool(plan and plan.uses_meter and meter),
    )


def _detail_context(request, work_order, complete_form=None):
    targets = allowed_transitions(work_order=work_order, membership=request.membership)
    return {
        "wo": work_order,
        "transitions": [(s, WorkOrder.Status(s).label) for s in targets if s != WorkOrder.Status.COMPLETED],
        "can_execute": has_perm(request.membership, Perm.WORK_ORDER_EXECUTE) and work_order.is_open,
        "complete_form": complete_form or _complete_form(work_order),
        "pending_required": sum(1 for i in work_order.checklist.all() if i.required and not i.result),
    }


def _get_detail_or_404(pk):
    try:
        return work_order_detail(pk)
    except WorkOrder.DoesNotExist as exc:
        raise Http404 from exc


@membership_required
def work_order_detail_view(request, pk):
    work_order = _get_detail_or_404(pk)
    return render(request, "work/detail.html", _detail_context(request, work_order))


@require_POST
@membership_required
def work_order_transition_view(request, pk):
    work_order = get_object_or_404(WorkOrder, pk=pk)
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
        WorkOrderChecklistItem.objects.select_related("work_order"), pk=item_pk, work_order_id=pk
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
    work_order = _get_detail_or_404(pk)
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
                meter_value=form.cleaned_data.get("meter_value"),
            )
        except ValidationError as exc:
            messages.error(request, " ".join(exc.messages))
        else:
            messages.success(request, f"{work_order.code} completada.")
            return redirect("work_order_detail", pk=pk)
    work_order = work_order_detail(pk)
    return render(request, "work/detail.html", _detail_context(request, work_order, form), status=400)


@membership_required
def attachment_view(request, pk):
    """Los archivos nunca se sirven públicamente: se verifica la organización."""
    attachment = get_object_or_404(Attachment, pk=pk)
    return FileResponse(
        attachment.file.open("rb"), content_type=attachment.content_type, filename=attachment.filename
    )
