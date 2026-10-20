import datetime
import uuid

from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.formats import date_format
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.translation import gettext_lazy as _
from django.views.decorators.http import require_POST

from apps.accounts.permissions import Perm, PermissionDeniedError, has_perm
from apps.attachments.models import Attachment, Purpose, TargetType
from apps.attachments.selectors import attachments_for
from apps.billing.access import can_act, is_read_only
from apps.core.decorators import membership_required
from apps.core.files import private_file_response
from apps.inventory.selectors import visible_warehouses
from apps.organizations.modules import ModuleNotIncludedError, has_module
from apps.ui.templatetags.ui import work_order_status_label

from .forms import (
    ApplyChecklistForm,
    AssigneesForm,
    BacklogFilterForm,
    CatalogPartUsageForm,
    ChecklistItemForm,
    ChecklistPhotoForm,
    ChecklistSignatureForm,
    CompleteWorkOrderForm,
    FailureCodeForm,
    FailureCodeRenameForm,
    LaborForm,
    LocationRequestForm,
    OtherCostForm,
    PartUsageForm,
    PlannedPartsForm,
    ScheduleForm,
    WorkOrderCreateForm,
)
from .models import FailureCode, FailureEvent, OtherCost, PartUsage, WorkOrder, WorkOrderChecklistItem
from .selectors import (
    anonymous_request_photos,
    assignable_members,
    backlog_summary,
    board_lanes,
    checklist_photos,
    checklist_plan_versions,
    failure_code_list,
    filter_backlog,
    filter_work_orders,
    open_requests_for_target,
    parts_for_work_order,
    pending_requests,
    planned_parts,
    scheduled_in_week,
    technician_load,
    technicians_in_scope,
    unscheduled_backlog,
    visible_requests,
    visible_work_orders,
    week_start,
    work_order_detail,
)
from .services import (
    COST_LOCKED_STATUSES,
    SCHEDULABLE,
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
    checklist_item_has_evidence,
    complete_work_order,
    create_corrective_from_item,
    create_failure_code,
    create_work_order,
    failure_started_default,
    log_labor,
    merge_request,
    move_checklist_item,
    reject_request,
    remove_checklist_item,
    remove_checklist_photo,
    remove_cost_line,
    rename_failure_code,
    report_failure,
    save_checklist_note,
    schedule_work_order,
    set_assignees,
    set_failure_code_active,
    transition,
    unschedule_work_order,
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
    query = filter_work_orders(params=request.GET, membership=request.membership)
    scope, work_type, filters, filter_form = query.scope, query.work_type, query.filters, query.filter_form
    qs = query.queryset
    page = Paginator(qs, 25).get_page(request.GET.get("page"))
    # «Buscar fecha en» solo indica dónde aplicar las fechas: no cuenta como filtro activo.
    active_filters = sum(1 for key, value in filters.items() if value and key != "date_field")
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
            "active_filters": active_filters,
            "filters_open": bool(active_filters or filter_form.errors),
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


def closing_assistant(request, work_order):
    """Conversación propia con el asistente sobre la OT, si el plan incluye el módulo (AI-04)."""
    if not work_order.is_open or not has_module(request.organization, "assistant"):
        return None
    from apps.assistant.selectors import closing_conversation

    return closing_conversation(membership=request.membership, work_order=work_order)


def _mark_assistant_helpful(request, assistant, helpful) -> None:
    """Marca "¿Te sirvió?" elegida al cerrar (AI-04 CA2); no impide completar la OT."""
    if assistant is None or not helpful or helpful == assistant.helpful:
        return
    from apps.assistant.services import mark_helpful

    try:
        mark_helpful(membership=request.membership, conversation=assistant, helpful=helpful)
    except (ValidationError, PermissionDeniedError):
        pass


def _complete_form(work_order, data=None, *, assistant=None, use_summary=True):
    meter = work_order_meter(work_order)
    plan = work_order.plan_assignment.plan if work_order.plan_assignment_id else None
    # Los eventos de falla son por activo: una correctiva sobre una ubicación no los pide.
    with_failure = work_order.work_type == WorkOrder.Type.CORRECTIVE and work_order.asset_id is not None
    initial = _failure_initial(work_order) if with_failure and data is None else {}
    if data is None and use_summary and assistant is not None and assistant.summary_status == "done":
        # Nota de cierre propuesta por el asistente (AI-04 CA1): pre-cargada y editable.
        initial = {**(initial or {}), "notes": assistant.summary}
    return CompleteWorkOrderForm(
        data,
        initial=initial or None,
        assistant=assistant,
        with_failure=with_failure,
        meter=meter,
        meter_required=bool(plan and plan.uses_meter and meter),
        require_failure_details=work_order.organization.require_failure_details,
        asset=work_order.asset,
    )


def _with_photos(items):
    """Adjunta a cada ítem sus fotos y su firma (una consulta) y un client_uuid para subir otra."""
    attachments = checklist_photos(items=items)
    correctives = {}  # correctivas nacidas de cada ítem (WO-06), en una consulta
    for child in WorkOrder.objects.filter(source_checklist_item__in=[i.pk for i in items]).order_by(
        "created_at"
    ):
        correctives.setdefault(child.source_checklist_item_id, []).append(child)
    for item in items:
        files = attachments.get(item.pk, [])
        item.photos = [a for a in files if a.purpose != Purpose.SIGNATURE]
        signatures = [a for a in files if a.purpose == Purpose.SIGNATURE]
        item.signature = signatures[-1] if signatures else None
        item.photo_uuid = uuid.uuid4()
        item.correctives = correctives.get(item.pk, [])
        if item.item_type == "signature":
            item.signature_form = ChecklistSignatureForm(auto_id=f"sig-{item.pk}-%s")
    return items


def inventory_enabled(request) -> bool:
    """Catálogo de repuestos en la OT: módulo `inventory` del plan e `inventory.view` (INV-16)."""
    return has_module(request.organization, "inventory") and has_perm(request.membership, Perm.INVENTORY_VIEW)


def parts_context(request, work_order, *, planned=None):
    """Repuestos previstos, buscador del catálogo y permisos de uso y reserva (INV-09, INV-10)."""
    membership = request.membership
    if not inventory_enabled(request):
        return {"inventory_enabled": False}
    can_use = (
        can_act(membership, Perm.WORK_ORDER_EXECUTE)
        and work_order.is_open
        and work_order.status not in COST_LOCKED_STATUSES
    )
    can_reserve = can_act(membership, Perm.WORK_ORDER_MANAGE) and work_order.status not in (
        WorkOrder.Status.COMPLETED,
        *WorkOrder.CLOSED_STATUSES,
    )
    if planned is None:
        planned = planned_parts(work_order=work_order)
    for planned_part in planned:
        planned_part.form_uuid = uuid.uuid4()  # un doble envío de "Usar" no duplica la línea
    query = request.GET.get("part_q", "").strip()[:100]
    results = []
    if can_use or can_reserve:
        results = parts_for_work_order(work_order=work_order, membership=membership, q=query)
        for part in results:
            part.form_uuid = uuid.uuid4()
    warehouses = list(visible_warehouses(membership=membership)) if can_use or can_reserve else []
    return {
        "inventory_enabled": True,
        "planned_parts": planned,
        "can_use_parts": can_use,
        "can_reserve": can_reserve,
        "part_query": query,
        "part_results": results,
        # Con una sola bodega no se ofrece elegirla (INV-03 CA5, INV-09 CA3).
        "warehouse_choices": warehouses if len(warehouses) > 1 else [],
    }


def _planned_form(request, work_order, planned, data=None):
    """Repuestos previstos pendientes pre-marcados al completar (Q2); None si no aplica."""
    if not inventory_enabled(request):
        return None
    return PlannedPartsForm(data, planned=planned)


def _detail_context(request, work_order, complete_form=None, assignees_form=None, planned_form=None):
    # Solo lectura (ORG-13): sin transiciones ni formularios; se ve todo lo registrado.
    read_only = is_read_only(request.organization)
    targets = [] if read_only else allowed_transitions(work_order=work_order, membership=request.membership)
    can_manage = can_act(request.membership, Perm.WORK_ORDER_MANAGE)
    can_assign = can_manage and work_order.status not in WorkOrder.CLOSED_STATUSES
    if can_assign and assignees_form is None:
        assignees_form = AssigneesForm(
            initial={"assigned_to": work_order.assigned_to_id, "helpers": list(work_order.helpers.all())},
            membership=request.membership,
        )
    attachments = list(attachments_for(target_type=TargetType.WORK_ORDER, target_id=work_order.pk))
    signatures = [a for a in attachments if a.purpose == Purpose.SIGNATURE]
    executor = has_perm(request.membership, Perm.WORK_ORDER_EXECUTE) and work_order.is_open
    can_execute = executor and not read_only
    pending_required = sum(1 for i in work_order.checklist.all() if i.required and not i.result)
    transitions = [s for s in targets if s != WorkOrder.Status.COMPLETED]
    parts = parts_context(request, work_order)
    assistant = closing_assistant(request, work_order) if executor else None
    use_summary = request.GET.get("summary") != "discard"
    if can_execute and planned_form is None and parts["inventory_enabled"]:
        planned_form = _planned_form(request, work_order, parts["planned_parts"])
    # Acción principal visible arriba: en ejecución, ir a completar (o al checklist si faltan
    # ítems obligatorios); si no, aprobar el cierre o iniciar/reanudar. El resto es secundario.
    complete_action = can_execute and work_order.status == WorkOrder.Status.IN_PROGRESS
    primary_transition = None
    if not complete_action:
        primary_transition = next(
            (s for s in (WorkOrder.Status.CLOSED, WorkOrder.Status.IN_PROGRESS) if s in transitions), None
        )
    return {
        "wo": work_order,
        "primary_transition": primary_transition,
        "complete_action": complete_action,
        "transitions": [(s, work_order_status_label(s)) for s in transitions if s != primary_transition],
        "helpers": list(work_order.helpers.all()),
        "assignees_form": assignees_form if can_assign else None,
        "can_execute": can_execute,
        "complete_form": complete_form
        or _complete_form(work_order, assistant=assistant, use_summary=use_summary),
        "assistant": assistant,
        "assistant_summary_used": assistant is not None
        and assistant.summary_status == "done"
        and (use_summary if complete_form is None else False),
        "pending_required": pending_required,
        "costs_editable": work_order.status not in COST_LOCKED_STATUSES,
        "can_manage": can_manage,
        "can_edit_checklist": can_manage and checklist_editable(work_order),
        "checklist_items": _with_photos(list(work_order.checklist.all())),
        "labor_form": LaborForm(),
        "part_form": PartUsageForm(),
        "other_cost_form": OtherCostForm(),
        "attachments": [a for a in attachments if a.purpose != Purpose.SIGNATURE],
        "signature": signatures[-1] if signatures else None,
        "source_item": _with_photos([work_order.source_checklist_item])[0]
        if work_order.source_checklist_item_id
        else None,
        "planned_form": planned_form if can_execute else None,
        **parts,
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


def _checklist_item_or_404(request, pk, item_pk):
    """Ítem de una OT visible para la membresía (alcance por sitio); 404 si no."""
    return get_object_or_404(
        WorkOrderChecklistItem.objects.select_related("work_order").filter(
            work_order__in=visible_work_orders(membership=request.membership)
        ),
        pk=item_pk,
        work_order_id=pk,
    )


def _checklist_item_response(request, item, error=None):
    """Tarjeta del ítem para htmx (tras responder, subir o quitar una foto)."""
    return render(
        request,
        "work/_checklist_item.html",
        {
            "item": _with_photos([item])[0],
            "error": error,
            "can_execute": item.work_order.is_open and not is_read_only(request.organization),
            "can_manage": can_act(request.membership, Perm.WORK_ORDER_MANAGE),
        },
    )


@require_POST
@membership_required(perm=Perm.WORK_ORDER_EXECUTE)
def checklist_answer_view(request, pk, item_pk):
    item = _checklist_item_or_404(request, pk, item_pk)
    if request.POST.get("note_only"):
        return _save_checklist_note(request, item)
    error, signature = None, None
    if item.item_type == "signature":
        form = ChecklistSignatureForm(request.POST)
        if form.is_valid():
            signature = form.cleaned_data["signature"]
    try:
        answer_checklist_item(
            item=item,
            membership=request.membership,
            result=request.POST.get("result", ""),
            value_number=request.POST.get("value_number") or None,
            value_text=request.POST.get("value_text", ""),
            signature=signature,
            note=request.POST.get("note", ""),
        )
    except WorkOrderError as exc:
        error = exc.message
    except PermissionDeniedError as exc:
        raise PermissionDenied from exc
    if request.headers.get("HX-Request"):
        response = _checklist_item_response(request, item, error)
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
    assistant = closing_assistant(request, work_order)
    form = _complete_form(work_order, request.POST, assistant=assistant)
    planned_form = _planned_form(request, work_order, planned_parts(work_order=work_order), request.POST)
    if form.is_valid() and (planned_form is None or planned_form.is_valid()):
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
                parts_used=planned_form.parts_used() if planned_form is not None else (),
            )
        except ModuleNotIncludedError as exc:
            messages.error(request, exc.message)
        except ValidationError as exc:
            messages.error(request, " ".join(exc.messages))
        else:
            messages.success(request, _("%(code)s completada.") % {"code": work_order.code})
            _mark_assistant_helpful(request, assistant, form.cleaned_data.get("assistant_helpful"))
            if work_order.signature_warning:  # cupo lleno: se completó sin la firma
                messages.warning(request, work_order.signature_warning)
            return redirect("work_order_detail", pk=pk)
    work_order = work_order_detail(pk, membership=request.membership)
    return render(
        request,
        "work/detail.html",
        _detail_context(request, work_order, form, planned_form=planned_form),
        status=400,
    )


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
            "items": _editable_checklist(work_order),
            "apply_form": apply_form,
            "item_form": item_form,
            "has_versions": bool(versions),
        },
        status=400 if form is not None else 200,
    )


@require_POST
@membership_required(perm=Perm.WORK_ORDER_EXECUTE)
def checklist_photo_add_view(request, pk, item_pk):
    item = _checklist_item_or_404(request, pk, item_pk)
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
        return _checklist_item_response(request, item, error)
    if error:
        messages.error(request, error)
    else:
        messages.success(request, _("Foto agregada."))
    return redirect(f"{reverse('work_order_detail', args=[pk])}#item-{item.pk}")


@require_POST
@membership_required(perm=Perm.WORK_ORDER_EXECUTE)
def checklist_photo_delete_view(request, pk, item_pk, photo_pk):
    """Quita una foto del ítem: quien la subió o un gestor, con la OT abierta (WO-03 CA2)."""
    item = _checklist_item_or_404(request, pk, item_pk)
    photo = get_object_or_404(
        Attachment.objects.filter(target_type=TargetType.CHECKLIST_ITEM, target_id=item.pk), pk=photo_pk
    )
    error = None
    try:
        remove_checklist_photo(item=item, photo=photo, membership=request.membership)
    except WorkOrderError as exc:
        error = exc.message
    except PermissionDeniedError as exc:
        raise PermissionDenied from exc
    if request.headers.get("HX-Request"):
        return _checklist_item_response(request, item, error)
    if error:
        messages.error(request, error)
    else:
        messages.success(request, _("Foto quitada."))
    return redirect(f"{reverse('work_order_detail', args=[pk])}#item-{item.pk}")


def _editable_checklist(work_order):
    """Ítems de la pantalla de checklist; `locked` = ya tiene respuesta, observación o fotos."""
    items = list(work_order.checklist.all())
    photos = checklist_photos(items=items)
    for item in items:
        item.locked = checklist_item_has_evidence(item, photo_count=len(photos.get(item.pk, [])))
    return items


def _checklist_items_changed(request, pk, *, error=None, success=None):
    """Tras quitar o mover: htmx recibe la lista actualizada; sin JS, vuelve a la pantalla."""
    if request.headers.get("HX-Request"):
        work_order = get_object_or_404(visible_work_orders(membership=request.membership), pk=pk)
        return render(
            request,
            "work/_checklist_edit_items.html",
            {"wo": work_order, "items": _editable_checklist(work_order), "error": error, "success": success},
        )
    if error:
        messages.error(request, error)
    elif success:
        messages.success(request, success)
    return redirect(f"{reverse('work_order_checklist', args=[pk])}#current-items")


@require_POST
@membership_required(perm=Perm.WORK_ORDER_MANAGE)
def checklist_item_remove_view(request, pk, item_pk):
    """Quita un ítem sin responder del checklist (WO-03).

    Con htmx la confirmación es `hx-confirm`; sin JS el botón llega sin `confirm=1` y
    se muestra una página de confirmación que vuelve a enviar con `confirm=1`.
    """
    item = _checklist_item_or_404(request, pk, item_pk)
    if not request.headers.get("HX-Request") and request.POST.get("confirm") != "1":
        return render(
            request, "work/checklist_item_remove_confirm.html", {"wo": item.work_order, "item": item}
        )
    try:
        remove_checklist_item(item=item, membership=request.membership)
    except WorkOrderError as exc:
        return _checklist_items_changed(request, pk, error=exc.message)
    except PermissionDeniedError as exc:
        raise PermissionDenied from exc
    return _checklist_items_changed(request, pk, success=_("Ítem «%(text)s» quitado.") % {"text": item.text})


@require_POST
@membership_required(perm=Perm.WORK_ORDER_MANAGE)
def checklist_item_move_view(request, pk, item_pk):
    """Sube o baja un ítem una posición (WO-03); botones, sin arrastrar."""
    item = _checklist_item_or_404(request, pk, item_pk)
    try:
        move_checklist_item(
            item=item, membership=request.membership, direction=request.POST.get("direction", "")
        )
    except WorkOrderError as exc:
        return _checklist_items_changed(request, pk, error=exc.message)
    except PermissionDeniedError as exc:
        raise PermissionDenied from exc
    return _checklist_items_changed(request, pk)


# --- Costos (WO-04) -----------------------------------------------------------


def _costs_redirect(pk):
    return redirect(f"{reverse('work_order_detail', args=[pk])}#costs")


def _run_cost_action(request, pk, action):
    try:
        action()
    except ModuleNotIncludedError as exc:
        messages.error(request, exc.message)
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
    if request.POST.get("part"):
        return _catalog_part_add(request, work_order)
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


def _catalog_part_add(request, work_order):
    """ "Usar" un repuesto del catálogo (INV-09): el servidor fija descripción, unidad y costo."""
    form = CatalogPartUsageForm(request.POST)
    if not form.is_valid():
        messages.error(request, _("Indica una cantidad mayor que cero."))
        return _costs_redirect(work_order.pk)
    data = form.cleaned_data

    def action():
        usage = add_part_usage(
            work_order=work_order,
            membership=request.membership,
            part=data["part"],
            quantity=data["quantity"],
            warehouse=data["warehouse"],
            client_uuid=data["client_uuid"],
        )
        messages.success(
            request,
            _("Usado: %(quantity)s %(unit)s de %(part)s.")
            % {"quantity": usage.quantity.normalize(), "unit": usage.unit, "part": usage.description},
        )

    return _run_cost_action(request, work_order.pk, action)


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


@membership_required(perm=Perm.WORK_ORDER_MANAGE)
def request_photo_view(request, pk):
    """Foto de un aviso anónimo (REQ-02) antes de aceptarlo: aún no tiene OT donde verla."""
    work_request = _get_request_or_404(request, pk)
    photo = anonymous_request_photos(work_request).first()
    if photo is None:
        raise Http404
    return private_file_response(photo.file, content_type=photo.content_type, filename=photo.filename)


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


# --- Correctiva desde un hallazgo (WO-06) ---------------------------------------


@membership_required(perm=Perm.REQUEST_CREATE)
def checklist_item_corrective_view(request, pk, item_pk):
    """Desde un ítem No OK: el gestor crea la correctiva; el técnico avisa la falla (prellenada)."""
    item = _checklist_item_or_404(request, pk, item_pk)
    parent = item.work_order
    photos = _with_photos([item])[0].photos
    if request.method == "POST":
        try:
            work_order = create_corrective_from_item(
                item=item,
                membership=request.membership,
                description=request.POST.get("description", "")[:2000],
                asset_stopped=bool(request.POST.get("asset_stopped")),
            )
        except WorkOrderError as exc:
            messages.error(request, exc.message)
        except PermissionDeniedError as exc:
            raise PermissionDenied from exc
        else:
            messages.success(request, _("%(code)s creada desde el hallazgo.") % {"code": work_order.code})
            return redirect("work_order_detail", pk=work_order.pk)
    return render(
        request,
        "work/corrective_from_item.html",
        {
            "item": item,
            "parent": parent,
            "photos": photos,
            "can_manage": has_perm(request.membership, Perm.WORK_ORDER_MANAGE),
        },
    )


# --- Backlog y programación semanal (REQ-04, REQ-05) -----------------------------

BOARD_BACKLOG_PAGE = 12


def _backlog_filters(request):
    form = BacklogFilterForm(request.GET, membership=request.membership)
    form.is_valid()  # los campos con error se ignoran y se muestran en el formulario
    return form, form.cleaned_data


@membership_required(perm=Perm.WORK_ORDER_MANAGE)
def backlog_view(request):
    """Backlog priorizado (REQ-04): puntaje, horas estimadas y backlog en semanas."""
    membership = request.membership
    qs, filter_form = filter_backlog(params=request.GET, membership=membership)
    technicians = technicians_in_scope(
        membership=membership, location=filter_form.cleaned_data.get("location")
    )
    active_filters = filter_form.active_count
    return render(
        request,
        "work/backlog.html",
        {
            "page": Paginator(qs, 25).get_page(request.GET.get("page")),
            "summary": backlog_summary(qs, technicians=technicians),
            "filter_form": filter_form,
            "active_filters": active_filters,
            "filters_open": bool(active_filters or filter_form.errors),
        },
    )


def _week_from(request):
    try:
        day = datetime.date.fromisoformat(request.GET.get("week", ""))
    except ValueError:
        day = timezone.localdate()
    return week_start(day)


def _week_context(request, monday, *, weekend: bool):
    days = [monday + datetime.timedelta(days=i) for i in range(7)]
    today = timezone.localdate()
    return {
        "monday": monday,
        "sunday": days[-1],
        "days": days,
        "visible_days": days if weekend else days[:5],
        "weekend": weekend,
        "today": today,
        "prev_query": _query_with(request, week=(monday - datetime.timedelta(days=7)).isoformat()),
        "next_query": _query_with(request, week=(monday + datetime.timedelta(days=7)).isoformat()),
        "this_week_query": _query_with(request, week=""),
        "weekend_query": _query_with(request, weekend="" if weekend else "1"),
        "is_this_week": monday == week_start(today),
    }


def _by_day(work_orders, days):
    grouped = {day: [] for day in days}
    for wo in work_orders:
        grouped.setdefault(wo.scheduled_for, []).append(wo)
    return [(day, grouped[day]) for day in days]


def _has_weekend(request, work_orders) -> bool:
    """El fin de semana se muestra si se pide o si ya hay OT programadas esos días."""
    return request.GET.get("weekend") == "1" or any(wo.scheduled_for.weekday() >= 5 for wo in work_orders)


@membership_required(perm=Perm.WORK_ORDER_MANAGE)
def board_view(request):
    """Tablero semanal (REQ-05): días con sus OT, backlog sin programar y carga por técnico."""
    membership = request.membership
    filter_form, filters = _backlog_filters(request)
    location = filters.get("location")
    monday = _week_from(request)
    work_orders = list(scheduled_in_week(membership=membership, monday=monday, location=location))
    context = _week_context(request, monday, weekend=_has_weekend(request, work_orders))
    technicians = technicians_in_scope(membership=membership, location=location)
    loads = technician_load(work_orders=work_orders, technicians=technicians, days=context["days"])
    backlog_page = Paginator(
        unscheduled_backlog(membership=membership, location=location), BOARD_BACKLOG_PAGE
    ).get_page(request.GET.get("page"))
    writable = not is_read_only(request.organization)  # solo lectura (ORG-13): nada se mueve
    for wo in [*work_orders, *backlog_page.object_list]:
        wo.movable = writable and wo.status in SCHEDULABLE
    for row in loads:
        row.visible = list(zip(context["visible_days"], row.days, strict=False))
    lanes_on = request.GET.get("lanes") == "1"
    context.update(
        {
            "columns": _by_day(work_orders, context["visible_days"]),
            "lanes_on": lanes_on,
            "lanes_query": _query_with(request, lanes="" if lanes_on else "1"),
            "lanes": board_lanes(
                work_orders=work_orders,
                loads=loads,
                days=context["days"],
                visible_days=context["visible_days"],
            )
            if lanes_on
            else [],
            "backlog_page": backlog_page,
            "loads": loads,
            "overloaded": sum(1 for row in loads for cell in row.days if cell.over),
            "members": list(assignable_members(membership=membership)),
            "filter_form": filter_form,
            "return_to": request.get_full_path(),
            # Día sugerido en «Programar» (sin JS): hoy, o el lunes si se mira una semana futura.
            "default_day": max(monday, context["today"]).isoformat(),
        }
    )
    return render(request, "work/board.html", context)


@membership_required(perm=Perm.WORK_ORDER_EXECUTE)
def my_week_view(request):
    """«Mi semana» (REQ-05): las OT programadas de la persona, solo lectura, con su carga."""
    membership = request.membership
    monday = _week_from(request)
    work_orders = list(scheduled_in_week(membership=membership, monday=monday, user=request.user))
    context = _week_context(request, monday, weekend=_has_weekend(request, work_orders))
    load = technician_load(work_orders=work_orders, technicians=[membership], days=context["days"])[0]
    context.update(
        {
            "columns": _by_day(work_orders, context["visible_days"]),
            "load": load,
            "count": len(work_orders),
        }
    )
    return render(request, "work/my_week.html", context)


def _board_response(request, *, error=None, success=None):
    """Al arrastrar (fetch): 204, o 400 con el mensaje. Sin JS: mensaje y de vuelta al tablero."""
    if request.headers.get("X-Board-Drop"):
        if error:
            return HttpResponse(error, status=400, content_type="text/plain; charset=utf-8")
        return HttpResponse(status=204)
    if error:
        messages.error(request, error)
    elif success:
        messages.success(request, success)
    target = request.POST.get("next", "")
    if not url_has_allowed_host_and_scheme(target, allowed_hosts={request.get_host()}):
        target = reverse("work_order_board")
    return redirect(target)


def _board_work_order_or_404(request, pk):
    return get_object_or_404(
        visible_work_orders(membership=request.membership).select_related("asset", "organization"), pk=pk
    )


@membership_required(perm=Perm.WORK_ORDER_MANAGE)
@require_POST
def work_order_schedule_view(request, pk):
    work_order = _board_work_order_or_404(request, pk)
    form = ScheduleForm(request.POST, membership=request.membership)
    if not form.is_valid():
        return _board_response(request, error=str(_("Elige un día y un responsable válidos.")))
    kwargs = {}
    if form.cleaned_data["assigned_to"] is not None:
        kwargs["assigned_to"] = form.cleaned_data["assigned_to"]
    try:
        schedule_work_order(
            work_order=work_order,
            membership=request.membership,
            scheduled_for=form.cleaned_data["scheduled_for"],
            **kwargs,
        )
    except WorkOrderError as exc:
        return _board_response(request, error=exc.message)
    return _board_response(
        request,
        success=_("%(code)s programada para el %(date)s.")
        % {"code": work_order.code, "date": date_format(work_order.scheduled_for, "l j")},
    )


@membership_required(perm=Perm.WORK_ORDER_MANAGE)
@require_POST
def work_order_unschedule_view(request, pk):
    work_order = _board_work_order_or_404(request, pk)
    try:
        unschedule_work_order(work_order=work_order, membership=request.membership)
    except WorkOrderError as exc:
        return _board_response(request, error=exc.message)
    return _board_response(request, success=_("%(code)s volvió al backlog.") % {"code": work_order.code})
