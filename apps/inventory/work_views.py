"""Repuestos en la OT (INV-09, INV-10): buscador del catálogo y reservas manuales.

Viven bajo /inventory/work-orders/ (módulo `inventory`, `PlanModuleMiddleware`). El
consumo ("Usar") entra por la vista de costos de la OT (`work_order_part_add`), que
llama al mismo servicio `work.services.add_part_usage`.
"""

from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.translation import gettext as _
from django.views.decorators.http import require_GET, require_POST

from apps.accounts.permissions import Perm, PermissionDeniedError
from apps.core.decorators import membership_required
from apps.work.forms import ReservationForm
from apps.work.selectors import visible_work_orders
from apps.work.views import parts_context

from . import services
from .models import Part, Reservation, Warehouse
from .selectors import can_see_warehouse


def _work_order_or_404(request, pk):
    return get_object_or_404(
        visible_work_orders(membership=request.membership).select_related("asset"), pk=pk
    )


def _parts_redirect(pk):
    return redirect(f"{reverse('work_order_detail', args=[pk])}#parts")


@require_GET
@membership_required(perm=Perm.INVENTORY_VIEW)
def work_order_part_search_view(request, pk):
    """Resultados del buscador de repuestos de la OT (htmx); sugeridos primero."""
    work_order = _work_order_or_404(request, pk)
    context = parts_context(request, work_order, planned=[])
    return render(request, "work/_part_results.html", {"wo": work_order, **context})


@require_POST
@membership_required(perm=Perm.WORK_ORDER_MANAGE)
def work_order_reserve_view(request, pk):
    """Reserva manual de un repuesto para la OT (INV-10 CA2); repetir suma a la reserva activa."""
    work_order = _work_order_or_404(request, pk)
    form = ReservationForm(request.POST)
    if not form.is_valid():
        messages.error(request, _("Indica una cantidad mayor que cero."))
        return _parts_redirect(pk)
    part = Part.objects.filter(pk=form.cleaned_data["part"], is_active=True).first()
    if part is None:
        messages.error(request, _("Repuesto no encontrado."))
        return _parts_redirect(pk)
    warehouse = None
    if form.cleaned_data["warehouse"]:
        warehouse = Warehouse.objects.filter(pk=form.cleaned_data["warehouse"], is_active=True).first()
        if warehouse is None or not can_see_warehouse(request.membership, warehouse):
            messages.error(request, _("Bodega no encontrada."))
            return _parts_redirect(pk)
    try:
        reservation = services.reserve_for_work_order(
            work_order=work_order,
            part=part,
            quantity=form.cleaned_data["quantity"],
            warehouse=warehouse,
            source=Reservation.Source.MANUAL,
            by=request.user,
        )
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
    else:
        messages.success(
            request,
            _("Reservado: %(quantity)s %(unit)s de %(part)s.")
            % {"quantity": reservation.quantity.normalize(), "unit": part.unit, "part": part},
        )
    return _parts_redirect(pk)


@require_POST
@membership_required(perm=Perm.WORK_ORDER_MANAGE)
def work_order_release_view(request, pk, reservation_pk):
    """Quita (libera) una reserva activa de la OT (INV-10, motivo `manual`)."""
    work_order = _work_order_or_404(request, pk)
    reservation = get_object_or_404(Reservation, pk=reservation_pk, work_order=work_order)
    try:
        services.release_reservation(membership=request.membership, reservation=reservation)
    except PermissionDeniedError as exc:
        raise PermissionDenied from exc
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
    else:
        messages.success(request, _("Reserva quitada: %(part)s.") % {"part": reservation.part})
    return _parts_redirect(pk)
