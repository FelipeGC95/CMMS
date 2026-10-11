"""Movimientos de stock en la web (INV-05, INV-06, INV-07, INV-12, INV-14): entrada, conteo,
transferencia, libro de movimientos, reposición y sus Excel.

Vistas delgadas: validan el formulario y llaman a los servicios de `services.py`; las
lecturas pasan por `selectors.py`. Cada formulario lleva un `client_uuid` oculto: un
doble envío devuelve el mismo movimiento (idempotencia del servicio).
"""

from urllib.parse import urlencode

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.http import Http404, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _

from apps.accounts.permissions import Perm, has_perm
from apps.core.decorators import membership_required
from apps.maintenance.models import format_number
from apps.organizations.modules import has_module, plan_message
from apps.reporting.excel import (
    MAX_EXPORT_ROWS,
    XLSX_CONTENT_TYPE,
    movements_xlsx,
    reorder_xlsx,
    stock_xlsx,
)

from . import services
from .forms import CountForm, MovementFilterForm, ReceiveForm, TransferForm, apply_errors
from .models import Part, StockItem
from .selectors import (
    STOCK_STATUSES,
    movement_list,
    part_search,
    reorder_list,
    stock_counters,
    stock_list,
    visible_warehouses,
)
from .views import PAGE_SIZE, _pick_warehouse

ACTION_ANOTHER = "another"


def _part_or_none(value, *, active_only=True):
    """Repuesto de la organización del contexto (un ID ajeno o mal formado → None)."""
    qs = Part.objects.filter(is_active=True) if active_only else Part.objects.all()
    try:
        return qs.filter(pk=value).first() if value else None
    except (ValueError, ValidationError):
        return None


def _posted_part(request):
    """(repuesto, error) del formulario enviado: el radio del buscador o el campo oculto."""
    value = request.POST.get("part", "")
    if not value:
        return None, _("Elige un repuesto.")
    part = _part_or_none(value)
    return part, "" if part is not None else _("Repuesto no encontrado.")


def _picker_context(request, part):
    """Buscador de repuesto (radios dentro del formulario, sin JS; htmx lo refresca al escribir).

    `keep`: los parámetros de la página sin el repuesto ni la búsqueda (para "Cambiar" y el
    buscador sin JS), así se conservan bodega y documento.
    """
    query = request.GET.get("q", "").strip()
    keep = request.GET.copy()
    for name in ("part", "q"):
        keep.pop(name, None)
    return {
        "picked": part,
        "query": query,
        "keep": keep,
        "results": part_search(membership=request.membership, q=query) if part is None and query else [],
    }


def _stock_by_warehouse(part, warehouses) -> list:
    """(bodega, existencia, disponible) del repuesto en las bodegas dadas; una consulta."""
    items = {
        item.warehouse_id: item for item in StockItem.objects.filter(part=part, warehouse__in=warehouses)
    }
    rows = []
    for warehouse in warehouses:
        item = items.get(warehouse.pk)
        rows.append((warehouse, item.on_hand if item else 0, item.available if item else 0))
    return rows


def _warehouse_or_default(form, warehouses):
    """Bodega elegida; con una sola visible, esa; sin ninguna, None (el servicio crea la por defecto)."""
    if "warehouse" in form.fields:
        return form.cleaned_data["warehouse"]
    return warehouses[0] if warehouses else None


# --- Entrada (INV-05) -----------------------------------------------------------


@membership_required(perm=Perm.INVENTORY_MOVE)
def receive_view(request):
    """Entrada de una línea (pantalla 3). "Guardar y registrar otra" conserva bodega y documento."""
    membership = request.membership
    warehouses = list(visible_warehouses(membership=membership))
    show_costs = has_perm(membership, Perm.INVENTORY_COST_VIEW)
    part_error = ""
    if request.method == "POST":
        form = ReceiveForm(request.POST, warehouses=warehouses, show_costs=show_costs)
        part, part_error = _posted_part(request)
        if form.is_valid() and part is not None:
            data = form.cleaned_data
            warehouse = _warehouse_or_default(form, warehouses)
            try:
                movement = services.receive_stock(
                    membership=membership,
                    part=part,
                    quantity=data["quantity"],
                    warehouse=warehouse,
                    unit_cost=data.get("unit_cost"),
                    reference=data["reference"],
                    supplier=data["supplier"],
                    note=data["note"],
                    occurred_at=data["occurred_at"],
                    client_uuid=data["client_uuid"],
                )
            except ValidationError as error:
                apply_errors(form, error)
            else:
                return _after_receive(request, movement, part, data, warehouse)
    else:
        part = _part_or_none(request.GET.get("part"))
        initial = {"reference": request.GET.get("reference", "")[:60]}
        if warehouse := _pick_warehouse(warehouses, request.GET.get("warehouse")):
            initial["warehouse"] = warehouse.pk
        form = ReceiveForm(initial=initial, warehouses=warehouses, show_costs=show_costs)
    return render(
        request,
        "inventory/receive.html",
        {"form": form, "part_error": part_error, **_picker_context(request, part)},
    )


def _after_receive(request, movement, part, data, warehouse):
    messages.success(
        request,
        _("Entrada registrada: %(part)s +%(quantity)s %(unit)s.")
        % {"part": part.code, "quantity": format_number(movement.quantity), "unit": part.unit},
    )
    cost = data.get("unit_cost")
    if cost is not None and cost == 0:
        messages.warning(
            request,
            _("Registraste costo 0: el costo promedio de %(part)s bajó (se diluye).") % {"part": part.code},
        )
    if request.POST.get("action") == ACTION_ANOTHER:
        query = {"reference": data["reference"]}
        if warehouse is not None:
            query["warehouse"] = str(warehouse.pk)
        return redirect(f"{reverse('inventory_receive')}?{urlencode(query)}")
    return redirect("inventory_part_detail", pk=part.pk)


# --- Conteo (INV-06) ------------------------------------------------------------


@membership_required(perm=Perm.INVENTORY_MANAGE)
def count_pick_view(request):
    """Elegir el repuesto a contar; al elegirlo lleva a su conteo."""
    part = None if request.GET.get("search") else _part_or_none(request.GET.get("part"))
    if part is not None:
        return redirect("inventory_part_count", pk=part.pk)
    return render(request, "inventory/count_pick.html", _picker_context(request, None))


@membership_required(perm=Perm.INVENTORY_MANAGE)
def part_count_view(request, pk):
    """Conteo de un repuesto (pantalla 4): ajusta la diferencia; sin diferencia, lo informa."""
    membership = request.membership
    part = _part_or_none(pk, active_only=False)
    if part is None:
        raise Http404
    warehouses = list(visible_warehouses(membership=membership))
    if request.method == "POST":
        form = CountForm(request.POST, warehouses=warehouses)
        if form.is_valid():
            data = form.cleaned_data
            try:
                movement = services.count_stock(
                    membership=membership,
                    part=part,
                    counted_quantity=data["counted_quantity"],
                    warehouse=_warehouse_or_default(form, warehouses),
                    note=data["note"],
                    client_uuid=data["client_uuid"],
                )
            except ValidationError as error:
                apply_errors(form, error)
            else:
                if movement is None:
                    messages.info(request, _("Sin diferencia: el conteo coincide con el sistema."))
                else:
                    messages.success(
                        request,
                        _("Ajuste registrado: %(sign)s%(quantity)s %(unit)s; saldo %(after)s.")
                        % {
                            "sign": "+" if movement.quantity > 0 else "",
                            "quantity": format_number(movement.quantity),
                            "unit": part.unit,
                            "after": format_number(movement.on_hand_after),
                        },
                    )
                return redirect("inventory_part_detail", pk=part.pk)
    else:
        initial = {}
        if warehouse := _pick_warehouse(warehouses, request.GET.get("warehouse")):
            initial["warehouse"] = warehouse.pk
        form = CountForm(initial=initial, warehouses=warehouses)
    return render(
        request,
        "inventory/count.html",
        {"form": form, "part": part, "stock": _stock_by_warehouse(part, warehouses)},
    )


# --- Transferencia (INV-07) -----------------------------------------------------


@membership_required(perm=Perm.INVENTORY_MOVE)
def transfer_view(request):
    """Transferencia entre bodegas (pantalla 4). Solo existe con más de una bodega activa."""
    membership = request.membership
    warehouses = list(visible_warehouses(membership=membership))
    if len(warehouses) < 2:
        messages.info(request, _("Para transferir se necesitan al menos dos bodegas activas."))
        return redirect("inventory_warehouse_list")
    part_error = ""
    if request.method == "POST":
        form = TransferForm(request.POST, warehouses=warehouses)
        part, part_error = _posted_part(request)
        if form.is_valid() and part is not None:
            data = form.cleaned_data
            try:
                services.transfer_stock(
                    membership=membership,
                    part=part,
                    from_warehouse=data["from_warehouse"],
                    to_warehouse=data["to_warehouse"],
                    quantity=data["quantity"],
                    note=data["note"],
                    client_uuid=data["client_uuid"],
                )
            except ValidationError as error:
                apply_errors(form, error)
            else:
                messages.success(
                    request,
                    _("Transferidas %(quantity)s %(unit)s de %(part)s: %(source)s → %(target)s.")
                    % {
                        "quantity": format_number(data["quantity"]),
                        "unit": part.unit,
                        "part": part.code,
                        "source": data["from_warehouse"].name,
                        "target": data["to_warehouse"].name,
                    },
                )
                return redirect("inventory_part_detail", pk=part.pk)
    else:
        part = _part_or_none(request.GET.get("part"))
        initial = {}
        if source := _pick_warehouse(warehouses, request.GET.get("from")):
            initial["from_warehouse"] = source.pk
            initial["to_warehouse"] = next(w.pk for w in warehouses if w.pk != source.pk)
        form = TransferForm(initial=initial, warehouses=warehouses)
    return render(
        request,
        "inventory/transfer.html",
        {
            "form": form,
            "part_error": part_error,
            "stock": _stock_by_warehouse(part, warehouses) if part else [],
            **_picker_context(request, part),
        },
    )


# --- Libro de movimientos (INV-14) y reposición (INV-12) --------------------------


def _movement_filters(request, warehouses):
    form = MovementFilterForm(request.GET or None, warehouses=warehouses)
    return form, form.filters()


@membership_required(perm=Perm.INVENTORY_VIEW)
def movement_list_view(request):
    """Libro de movimientos (pantalla 5), paginado y filtrado; costos solo con cost_view."""
    membership = request.membership
    warehouses = list(visible_warehouses(membership=membership, include_inactive=True))
    form, filters = _movement_filters(request, warehouses)
    movements = movement_list(membership=membership, **filters)
    page = Paginator(movements, PAGE_SIZE).get_page(request.GET.get("page"))
    active = form.active_count if form.is_bound else 0
    return render(
        request,
        "inventory/movements.html",
        {
            "page": page,
            "filter_form": form,
            "active_filters": active,
            "filters_open": bool(active or form.errors),
            "multi_warehouse": len(warehouses) > 1,
            "show_costs": has_perm(membership, Perm.INVENTORY_COST_VIEW),
            "can_export": has_module(request.organization, "exports"),
        },
    )


@membership_required(perm=Perm.INVENTORY_VIEW)
def reorder_view(request):
    """Lista de reposición por bodega (pantalla 5): primero negativos y reservas sin cubrir."""
    membership = request.membership
    warehouses = list(visible_warehouses(membership=membership))
    warehouse = _pick_warehouse(warehouses, request.GET.get("warehouse")) if len(warehouses) > 1 else None
    page = Paginator(reorder_list(membership=membership, warehouse=warehouse), PAGE_SIZE).get_page(
        request.GET.get("page")
    )
    return render(
        request,
        "inventory/reorder.html",
        {
            "page": page,
            "warehouse": warehouse,
            "warehouses": warehouses,
            "counters": stock_counters(membership=membership, warehouse=warehouse),
            "can_move": has_perm(membership, Perm.INVENTORY_MOVE),
            "can_export": has_module(request.organization, "exports"),
        },
    )


# --- Excel (INV-14 CA5): módulos `exports` (middleware) + `inventory` (aquí) ----------


def _inventory_module_denied(request):
    if has_module(request.organization, "inventory"):
        return None
    return render(
        request,
        "organizations/plan_module.html",
        {"message": plan_message(request.organization, "inventory")},
        status=403,
    )


def _xlsx(request, *, queryset, build, back, prefix):
    """Excel con los filtros de la pantalla, o de vuelta a ella si supera el tope de filas."""
    total = queryset.count()
    if total > MAX_EXPORT_ROWS:
        messages.error(
            request,
            _("Son %(total)s filas y un Excel admite hasta %(max)s. Filtra la lista y exporta por partes.")
            % {"total": total, "max": MAX_EXPORT_ROWS},
        )
        query = request.GET.urlencode()
        return redirect(f"{reverse(back)}?{query}" if query else reverse(back))
    response = HttpResponse(build(queryset), content_type=XLSX_CONTENT_TYPE)
    filename = f"{prefix}-{request.organization.slug}-{timezone.localdate():%Y%m%d}.xlsx"
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


@membership_required(perm=Perm.INVENTORY_VIEW)
def stock_xlsx_view(request):
    """Stock a Excel: mismos filtros que el inicio de inventario (búsqueda, categoría, bodega, estado)."""
    if denied := _inventory_module_denied(request):
        return denied
    membership = request.membership
    warehouses = list(visible_warehouses(membership=membership))
    warehouse = _pick_warehouse(warehouses, request.GET.get("warehouse")) if len(warehouses) > 1 else None
    status = request.GET.get("status", "")
    parts = stock_list(
        membership=membership,
        q=request.GET.get("q", ""),
        category=request.GET.get("category", ""),
        warehouse=warehouse,
        status=status if status in STOCK_STATUSES else "",
    )
    show_costs = has_perm(membership, Perm.INVENTORY_COST_VIEW)
    return _xlsx(
        request,
        queryset=parts,
        build=lambda qs: stock_xlsx(qs, show_costs=show_costs),
        back="inventory_home",
        prefix="stock",
    )


@membership_required(perm=Perm.INVENTORY_VIEW)
def movements_xlsx_view(request):
    """Movimientos a Excel con los filtros aplicados en la pantalla."""
    if denied := _inventory_module_denied(request):
        return denied
    membership = request.membership
    warehouses = list(visible_warehouses(membership=membership, include_inactive=True))
    _form, filters = _movement_filters(request, warehouses)
    show_costs = has_perm(membership, Perm.INVENTORY_COST_VIEW)
    return _xlsx(
        request,
        queryset=movement_list(membership=membership, **filters),
        build=lambda qs: movements_xlsx(qs, show_costs=show_costs),
        back="inventory_movements",
        prefix="movimientos",
    )


@membership_required(perm=Perm.INVENTORY_VIEW)
def reorder_xlsx_view(request):
    """Lista de reposición a Excel (para comprar): por bodega, con el sugerido."""
    if denied := _inventory_module_denied(request):
        return denied
    membership = request.membership
    warehouses = list(visible_warehouses(membership=membership))
    warehouse = _pick_warehouse(warehouses, request.GET.get("warehouse")) if len(warehouses) > 1 else None
    show_costs = has_perm(membership, Perm.INVENTORY_COST_VIEW)
    return _xlsx(
        request,
        queryset=reorder_list(membership=membership, warehouse=warehouse),
        build=lambda qs: reorder_xlsx(qs, show_costs=show_costs),
        back="inventory_reorder",
        prefix="reposicion",
    )
