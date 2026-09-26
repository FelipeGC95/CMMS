import uuid

from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.views.decorators.http import require_POST

from apps.accounts.models import Membership
from apps.accounts.permissions import Perm, PermissionDeniedError, has_perm
from apps.attachments.models import Purpose, TargetType
from apps.attachments.selectors import attachments_for
from apps.core.decorators import membership_required
from apps.ui.templatetags.ui import work_order_status_label

from .forms import (
    ApplyChecklistForm,
    AssigneesForm,
    ChecklistItemForm,
    ChecklistPhotoForm,
    CompleteWorkOrderForm,
    FailureCodeForm,
    FailureCodeRenameForm,
    LaborForm,
    LocationRequestForm,
    OtherCostForm,
    PartUsageForm,
    WorkOrderCreateForm,
    WorkOrderFilterForm,
)
from .models import FailureCode, FailureEvent, OtherCost, PartUsage, WorkOrder, WorkOrderChecklistItem
from .selectors import (
    checklist_photos,
    checklist_plan_versions,
    failure_code_list,
    open_requests_for_target,
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
    add_checklist_item,
    add_checklist_photo,
    add_other_cost,
    add_part_usage,
    allowed_transitions,
    answer_checklist_item,
    apply_plan_checklist,
    checklist_editable,
    complete_work_order,
    create_failure_code,
    create_work_order,
    failure_started_default,
    log_labor,
    merge_request,
    reject_request,
    remove_cost_line,
    rename_failure_code,
    report_failure,
    save_checklist_note,
    set_assignees,
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
    # Precarga por querystring (p. ej. desde una medición fuera de rango, WO-03 CA2).
    keys = ("work_type", "asset", "location", "title")
    initial = {key: request.GET[key] for key in keys if request.GET.get(key)}
    form = WorkOrderCreateForm(request.POST or None, initial=initial, membership=membership)
    if request.method == "POST" and form.is_valid():
        try:
            work_order = create_work_order(by=request.user, **form.cleaned_data)
        except WorkOrderError as exc:
            form.add_error(None, exc.message)
        else:
            messages.success(request, _("%(code)s creada.") % {"code": work_order.code})
            return redirect("work_order_detail", pk=work_order.pk)
    return render(request, "work/create.html", {"form": form})


def _failure_initial(work_order):
    """Valores iniciales de la falla al cerrar: los del evento abierto por el aviso, si existe."""
    failure = FailureEvent.objects.filter(work_order=work_order).first()
    if failure is None:
        return {
            "impact": FailureEvent.Impact.NONE,
            "failure_started_at": failure_started_default(work_order),
            "failure_ended_at": timezone.now(),
            "symptom": work_order.description,
        }
    return {
        "impact": failure.impact,
        "failure_started_at": failure.started_at,
        "failure_ended_at": failure.ended_at or timezone.now(),
        "symptom": failure.description,
        "failure_mode": failure.failure_mode_id,
        "failure_cause": failure.failure_cause_id,
        "failure_mechanism": failure.failure_mechanism_id,
        "detection_method": failure.detection_method_id,
        "component": failure.component_id,
    }


def _complete_form(work_order, data=None):
    meter = work_order_meter(work_order)
    plan = work_order.plan_assignment.plan if work_order.plan_assignment_id else None
    # Los eventos de falla son por activo: una correctiva sobre una ubicación no los pide.
    with_failure = work_order.work_type == WorkOrder.Type.CORRECTIVE and work_order.asset_id is not None
    return CompleteWorkOrderForm(
        data,
        initial=_failure_initial(work_order) if with_failure and data is None else None,
        with_failure=with_failure,
        meter=meter,
        meter_required=bool(plan and plan.uses_meter and meter),
        require_failure_details=work_order.organization.require_failure_details,
        asset=work_order.asset,
    )


def _with_photos(items):
    """Adjunta a cada ítem sus fotos (una consulta) y un client_uuid para subir otra."""
    photos = checklist_photos(items=items)
    for item in items:
        item.photos = photos.get(item.pk, [])
        item.photo_uuid = uuid.uuid4()
    return items


def _detail_context(request, work_order, complete_form=None, assignees_form=None):
    targets = allowed_transitions(work_order=work_order, membership=request.membership)
    can_manage = has_perm(request.membership, Perm.WORK_ORDER_MANAGE)
    can_assign = can_manage and work_order.status not in WorkOrder.CLOSED_STATUSES
    if can_assign and assignees_form is None:
        assignees_form = AssigneesForm(
            initial={"assigned_to": work_order.assigned_to_id, "helpers": list(work_order.helpers.all())},
            membership=request.membership,
        )
    attachments = list(attachments_for(target_type=TargetType.WORK_ORDER, target_id=work_order.pk))
    signatures = [a for a in attachments if a.purpose == Purpose.SIGNATURE]
    return {
        "wo": work_order,
        "transitions": [(s, work_order_status_label(s)) for s in targets if s != WorkOrder.Status.COMPLETED],
        "helpers": list(work_order.helpers.all()),
        "assignees_form": assignees_form if can_assign else None,
        "can_execute": has_perm(request.membership, Perm.WORK_ORDER_EXECUTE) and work_order.is_open,
        "complete_form": complete_form or _complete_form(work_order),
        "pending_required": sum(1 for i in work_order.checklist.all() if i.required and not i.result),
        "costs_editable": work_order.status not in COST_LOCKED_STATUSES,
        "can_manage": can_manage,
        "can_edit_checklist": can_manage and checklist_editable(work_order),
        "checklist_items": _with_photos(list(work_order.checklist.all())),
        "labor_form": LaborForm(),
        "part_form": PartUsageForm(),
        "other_cost_form": OtherCostForm(),
        "attachments": [a for a in attachments if a.purpose != Purpose.SIGNATURE],
        "signature": signatures[-1] if signatures else None,
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
        messages.success(
            request,
            _("%(code)s: %(status)s.")
            % {"code": work_order.code, "status": work_order_status_label(work_order.status)},
        )
    return redirect("work_order_detail", pk=pk)


@require_POST
@membership_required(perm=Perm.WORK_ORDER_MANAGE)
def work_order_assignees_view(request, pk):
    """Cambia el responsable principal y los de apoyo (WO-01 CA2)."""
    work_order = _get_detail_or_404(request, pk)
    form = AssigneesForm(request.POST, membership=request.membership)
    if form.is_valid():
        try:
            set_assignees(
                work_order=work_order,
                membership=request.membership,
                assigned_to=form.cleaned_data["assigned_to"],
                helpers=form.cleaned_data["helpers"],
            )
        except WorkOrderError as exc:
            messages.error(request, exc.message)
        except PermissionDeniedError as exc:
            raise PermissionDenied from exc
        else:
            messages.success(request, _("Responsables actualizados."))
            return redirect("work_order_detail", pk=pk)
    work_order = work_order_detail(pk, membership=request.membership)
    return render(
        request, "work/detail.html", _detail_context(request, work_order, assignees_form=form), status=400
    )


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
    if request.POST.get("note_only"):
        return _save_checklist_note(request, item)
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
            request,
            "work/_checklist_item.html",
            {
                "item": _with_photos([item])[0],
                "error": error,
                "can_execute": True,
                "can_manage": has_perm(request.membership, Perm.WORK_ORDER_MANAGE),
            },
        )
        # La cabecera de la OT puede cambiar (pasa a "En ejecución").
        response["HX-Trigger"] = "checklist-changed"
        return response
    if error:
        messages.error(request, error)
    return redirect("work_order_detail", pk=pk)


def _save_checklist_note(request, item):
    """Guardado automático de la observación del ítem (WO-03 CA2), sin tocar el resultado."""
    error = None
    try:
        save_checklist_note(item=item, membership=request.membership, note=request.POST.get("note", ""))
    except WorkOrderError as exc:
        error = exc.message
    except PermissionDeniedError as exc:
        raise PermissionDenied from exc
    if request.headers.get("HX-Request"):
        return render(request, "work/_checklist_note_status.html", {"item": item, "error": error})
    if error:
        messages.error(request, error)
    return redirect("work_order_detail", pk=item.work_order_id)


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
                impact=form.cleaned_data.get("impact"),
                failure_started_at=form.cleaned_data.get("failure_started_at"),
                failure_ended_at=form.cleaned_data.get("failure_ended_at"),
                symptom=form.cleaned_data.get("symptom"),
                meter_value=form.cleaned_data.get("meter_value"),
                signature=form.cleaned_data.get("signature"),
            )
        except ValidationError as exc:
            messages.error(request, " ".join(exc.messages))
        else:
            messages.success(request, _("%(code)s completada.") % {"code": work_order.code})
            return redirect("work_order_detail", pk=pk)
    work_order = work_order_detail(pk, membership=request.membership)
    return render(request, "work/detail.html", _detail_context(request, work_order, form), status=400)


# --- Checklist de OT no preventivas y fotos por ítem (WO-03) -------------------


@membership_required(perm=Perm.WORK_ORDER_MANAGE)
def work_order_checklist_view(request, pk):
    """Agregar checklist a una OT no preventiva: desde un plan publicado o ítems propios."""
    work_order = get_object_or_404(
        visible_work_orders(membership=request.membership).select_related("asset", "location"), pk=pk
    )
    if not checklist_editable(work_order):
        messages.error(request, _("El checklist de esta OT ya no se puede cambiar."))
        return redirect("work_order_detail", pk=pk)
    versions = list(checklist_plan_versions(work_order=work_order))
    action = request.POST.get("action") if request.method == "POST" else None
    apply_form = ApplyChecklistForm(
        request.POST if action == "apply" else None, versions=versions, prefix="apply"
    )
    item_form = ChecklistItemForm(request.POST if action == "item" else None, prefix="item")
    form = {"apply": apply_form, "item": item_form}.get(action)
    if form is not None and form.is_valid():
        try:
            if action == "apply":
                items = apply_plan_checklist(
                    work_order=work_order,
                    plan_version=form.cleaned_data["plan_version"],
                    membership=request.membership,
                )
                messages.success(
                    request,
                    _("Se agregaron %(count)s ítems del plan.") % {"count": len(items)},
                )
            else:
                add_checklist_item(work_order=work_order, membership=request.membership, **form.cleaned_data)
                messages.success(request, _("Ítem agregado."))
        except WorkOrderError as exc:
            messages.error(request, exc.message)
        except PermissionDeniedError as exc:
            raise PermissionDenied from exc
        else:
            return redirect("work_order_checklist", pk=pk)
    return render(
        request,
        "work/checklist_edit.html",
        {
            "wo": work_order,
            "items": work_order.checklist.all(),
            "apply_form": apply_form,
            "item_form": item_form,
            "has_versions": bool(versions),
        },
        status=400 if form is not None else 200,
    )


@require_POST
@membership_required(perm=Perm.WORK_ORDER_EXECUTE)
def checklist_photo_add_view(request, pk, item_pk):
    item = get_object_or_404(
        WorkOrderChecklistItem.objects.select_related("work_order").filter(
            work_order__in=visible_work_orders(membership=request.membership)
        ),
        pk=item_pk,
        work_order_id=pk,
    )
    form = ChecklistPhotoForm(request.POST, request.FILES)
    error = None
    if not form.is_valid():
        error = _("Elige una foto.")
    else:
        try:
            add_checklist_photo(
                item=item,
                membership=request.membership,
                uploaded_file=form.cleaned_data["photo"],
                client_uuid=form.cleaned_data.get("client_uuid"),
            )
        except WorkOrderError as exc:
            error = exc.message
        except PermissionDeniedError as exc:
            raise PermissionDenied from exc
    if request.headers.get("HX-Request"):
        return render(
            request,
            "work/_checklist_item.html",
            {
                "item": _with_photos([item])[0],
                "error": error,
                "can_execute": True,
                "can_manage": has_perm(request.membership, Perm.WORK_ORDER_MANAGE),
            },
        )
    if error:
        messages.error(request, error)
    else:
        messages.success(request, _("Foto agregada."))
    return redirect(f"{reverse('work_order_detail', args=[pk])}#item-{item.pk}")


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
        messages.error(request, _("Indica los minutos trabajados (1 a 1.440)."))
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
            request, _("Revisa el repuesto: descripción, cantidad y costo unitario son obligatorios.")
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
        messages.error(request, _("Revisa el costo: descripción y monto son obligatorios."))
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


# --- Solicitudes (REQ-01) --------------------------------------------------------


@membership_required(perm=Perm.REQUEST_CREATE)
def location_report_view(request):
    """Avisar un problema en una ubicación (REQ-01 CA1); el de un activo está en su ficha."""
    membership = request.membership
    initial = {"location": request.GET["location"]} if request.GET.get("location") else None
    form = LocationRequestForm(
        request.POST or None, request.FILES or None, initial=initial, membership=membership
    )
    if request.method == "POST" and form.is_valid():
        try:
            work_request = report_failure(
                membership=membership,
                location=form.cleaned_data["location"],
                description=form.cleaned_data["description"],
                photo=form.cleaned_data.get("photo"),
                client_uuid=form.cleaned_data.get("client_uuid"),
            )
        except PermissionDeniedError as exc:
            raise PermissionDenied from exc
        except WorkOrderError as exc:
            form.add_error(None, exc.message)
        else:
            messages.success(
                request, _("Aviso enviado. Se creó la %(code)s.") % {"code": work_request.work_order.code}
            )
            if getattr(work_request, "photo_warning", ""):  # cupo lleno: el aviso va sin foto
                messages.warning(request, work_request.photo_warning)
            return redirect("work_order_detail", pk=work_request.work_order_id)
    return render(request, "work/location_report.html", {"form": form})


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
        messages.success(request, _("Solicitud #%(number)s aceptada.") % {"number": work_request.number})
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
        messages.success(request, _("Solicitud #%(number)s rechazada.") % {"number": work_request.number})
    return redirect("request_list")


@membership_required(perm=Perm.WORK_ORDER_MANAGE)
def request_merge_view(request, pk):
    work_request = _get_request_or_404(request, pk)
    candidates = open_requests_for_target(work_request=work_request, membership=request.membership)
    if request.method == "POST":
        into = get_object_or_404(candidates, pk=request.POST.get("into", ""))
        try:
            merge_request(work_request=work_request, into=into, membership=request.membership)
        except WorkOrderError as exc:
            messages.error(request, exc.message)
        else:
            messages.success(
                request,
                _("Solicitud #%(number)s fusionada con #%(into)s.")
                % {"number": work_request.number, "into": into.number},
            )
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
            messages.success(request, _("Código agregado."))
    else:
        messages.error(request, _("Revisa el tipo, código y nombre."))
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
            messages.success(request, _("%(code)s actualizado.") % {"code": failure_code.code})
    else:
        messages.error(request, _("El nombre es obligatorio."))
    return redirect("failure_code_list")


@require_POST
@membership_required(perm=Perm.WORK_ORDER_MANAGE)
def failure_code_toggle_view(request, pk):
    failure_code = get_object_or_404(FailureCode, pk=pk)
    set_failure_code_active(
        failure_code=failure_code, is_active=not failure_code.is_active, membership=request.membership
    )
    return redirect("failure_code_list")
