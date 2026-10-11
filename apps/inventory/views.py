"""Pantallas web de inventario (INV-01, INV-03, INV-04, INV-12): delgadas, sobre servicios y selectores."""

import re

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.http import Http404
from django.shortcuts import redirect, render
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from apps.accounts.permissions import Perm, has_perm
from apps.core.decorators import membership_required

from . import services
from .forms import PartForm, StockLevelsForm, WarehouseForm, apply_errors
from .models import Part
from .selectors import (
    STOCK_STATUSES,
    part_categories,
    part_detail,
    part_search,
    stock_counters,
    stock_list,
    stock_rows,
    visible_warehouses,
)

PAGE_SIZE = 25
PICK_NAME = re.compile(r"^[a-z_]{1,30}$")


def _perms(membership) -> dict:
    return {
        "can_manage": has_perm(membership, Perm.INVENTORY_MANAGE),
        "can_move": has_perm(membership, Perm.INVENTORY_MOVE),
        "show_costs": has_perm(membership, Perm.INVENTORY_COST_VIEW),
    }


def _pick_warehouse(warehouses, value):
    """Bodega elegida en un filtro o formulario, solo entre las visibles (un ID ajeno no se encuentra)."""
    return next((warehouse for warehouse in warehouses if str(warehouse.pk) == (value or "")), None)


def _get_part(pk):
    part = Part.objects.filter(pk=pk).first()
    if part is None:
        raise Http404
    return part


def _get_warehouse(membership, pk):
    warehouse = visible_warehouses(membership=membership, include_inactive=True).filter(pk=pk).first()
    if warehouse is None:
        raise Http404
    return warehouse


# --- Inicio y stock -------------------------------------------------------------


@membership_required(perm=Perm.INVENTORY_VIEW)
def inventory_home_view(request):
    """Inicio de inventario (pantalla 1): buscador, fichas contador y stock por repuesto."""
    membership = request.membership
    warehouses = list(visible_warehouses(membership=membership))
    warehouse = _pick_warehouse(warehouses, request.GET.get("warehouse")) if len(warehouses) > 1 else None
    status = request.GET.get("status", "")
    if status not in STOCK_STATUSES:
        status = ""
    search = request.GET.get("q", "").strip()
    category = request.GET.get("category", "").strip()
    parts = stock_list(membership=membership, q=search, category=category, warehouse=warehouse, status=status)
    page = Paginator(parts, PAGE_SIZE).get_page(request.GET.get("page"))
    return render(
        request,
        "inventory/home.html",
        {
            "page": page,
            "search": search,
            "category": category,
            "status": status,
            "warehouse": warehouse,
            "warehouses": warehouses,
            "categories": part_categories(),
            "counters": stock_counters(membership=membership, warehouse=warehouse),
            "has_parts": Part.objects.exists(),
            "filtered": bool(search or category or status or warehouse),
            **_perms(membership),
        },
    )


@membership_required(perm=Perm.INVENTORY_VIEW)
def part_search_view(request):
    """Buscador htmx de repuestos activos (parcial reutilizable por la OT, entradas y conteos).

    `?pick=<campo>` devuelve opciones de radio con ese nombre (se eligen dentro del
    formulario que contiene el buscador, sin JS); sin `pick`, enlaces a la ficha.
    """
    pick = request.GET.get("pick", "")
    query = request.GET.get("q", "").strip()
    return render(
        request,
        "inventory/_part_search_results.html",
        {
            "results": part_search(membership=request.membership, q=query),
            "query": query,
            "pick": pick if PICK_NAME.match(pick) else "",
        },
    )


# --- Repuestos ------------------------------------------------------------------


def _part_form_context(form, part=None):
    return {"form": form, "part": part, "categories": part_categories()}


@membership_required(perm=Perm.INVENTORY_MANAGE)
def part_create_view(request):
    form = PartForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            part = services.create_part(membership=request.membership, **form.cleaned_data)
        except ValidationError as error:
            apply_errors(form, error)
        else:
            messages.success(request, _("Repuesto %(code)s creado.") % {"code": part.code})
            return redirect("inventory_part_detail", pk=part.pk)
    return render(request, "inventory/part_form.html", _part_form_context(form))


@membership_required(perm=Perm.INVENTORY_MANAGE)
def part_edit_view(request, pk):
    part = _get_part(pk)
    form = PartForm.for_part(part, request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            services.update_part(membership=request.membership, part=part, **form.cleaned_data)
        except ValidationError as error:
            apply_errors(form, error)
        else:
            messages.success(request, _("Cambios guardados."))
            return redirect("inventory_part_detail", pk=part.pk)
    return render(request, "inventory/part_form.html", _part_form_context(form, part))


def _plain(value):
    """Cantidad sin ceros sobrantes para el campo: 4.000 se ve «4» (no «4,000», que se lee cuatro mil)."""
    if value is None:
        return None
    return value.quantize(1) if value == value.to_integral() else value.normalize()


def _levels_forms(rows, *, bound=None):
    """Un formulario de mínimo/objetivo/ubicación por bodega activa (IDs únicos por bodega).

    `bound`: (bodega, formulario ya validado) que reemplaza al de esa bodega, con sus errores.
    """
    forms = []
    for row in rows:
        if not row.warehouse.is_active:
            continue
        if bound is not None and bound[0].pk == row.warehouse.pk:
            forms.append((row.warehouse, bound[1], bound[2]))
            continue
        item = row.item
        initial = {
            "min_quantity": _plain(item.min_quantity) if item else 0,
            "target_quantity": _plain(item.target_quantity) if item else None,
            "bin_location": item.bin_location if item else "",
        }
        form = StockLevelsForm(initial=initial, auto_id=f"levels-{row.warehouse.pk}-%s")
        forms.append((row.warehouse, form, False))
    return forms


def _detail_context(membership, pk, *, bound=None) -> dict:
    try:
        detail = part_detail(pk, membership=membership)
    except Part.DoesNotExist:
        raise Http404 from None
    warehouses = list(visible_warehouses(membership=membership))
    rows = stock_rows(detail, warehouses=warehouses)
    perms = _perms(membership)
    return {
        "detail": detail,
        "part": detail.part,
        "rows": rows,
        "multi_warehouse": len(warehouses) > 1,
        "levels_forms": _levels_forms(rows, bound=bound) if perms["can_manage"] else [],
        **perms,
    }


@membership_required(perm=Perm.INVENTORY_VIEW)
def part_detail_view(request, pk):
    """Ficha del repuesto (pantalla 2, INV-04): stock por bodega, mínimos y últimos movimientos."""
    return render(request, "inventory/part_detail.html", _detail_context(request.membership, pk))


@require_POST
@membership_required(perm=Perm.INVENTORY_MANAGE)
def part_levels_view(request, pk):
    """Guarda mínimo, objetivo y ubicación en bodega (htmx: devuelve la sección de stock al día)."""
    membership = request.membership
    part = _get_part(pk)
    warehouse = _pick_warehouse(visible_warehouses(membership=membership), request.POST.get("warehouse"))
    if warehouse is None:
        raise Http404
    form = StockLevelsForm(request.POST, auto_id=f"levels-{warehouse.pk}-%s")
    saved = False
    if form.is_valid():
        try:
            services.set_stock_levels(
                membership=membership,
                part=part,
                warehouse=warehouse,
                min_quantity=form.cleaned_data["min_quantity"],
                target_quantity=form.cleaned_data["target_quantity"],
                bin_location=form.cleaned_data["bin_location"],
            )
        except ValidationError as error:
            apply_errors(form, error)
        else:
            saved = True
    if request.headers.get("HX-Request"):
        bound = None if saved else (warehouse, form, False)
        context = _detail_context(membership, part.pk, bound=bound)
        if saved:
            context["levels_forms"] = [
                (w, f, w.pk == warehouse.pk) for w, f, _saved in context["levels_forms"]
            ]
        return render(request, "inventory/_stock_section.html", context)
    if saved:
        messages.success(request, _("Mínimo y ubicación guardados."))
    else:
        for errors in form.errors.values():
            messages.error(request, " ".join(errors))
    return redirect("inventory_part_detail", pk=part.pk)


@require_POST
@membership_required(perm=Perm.INVENTORY_MANAGE)
def part_active_view(request, pk):
    """Desactiva (sale de los buscadores; el historial sigue) o reactiva un repuesto (INV-01 CA3)."""
    part = _get_part(pk)
    is_active = request.POST.get("is_active") == "1"
    services.set_part_active(membership=request.membership, part=part, is_active=is_active)
    if is_active:
        messages.success(request, _("Repuesto %(code)s reactivado.") % {"code": part.code})
    else:
        messages.success(
            request,
            _("Repuesto %(code)s desactivado: ya no aparece en los buscadores.") % {"code": part.code},
        )
    return redirect("inventory_part_detail", pk=part.pk)


# --- Bodegas --------------------------------------------------------------------


@membership_required(perm=Perm.INVENTORY_VIEW)
def warehouse_list_view(request):
    """Bodegas (pantalla 6, INV-03): por defecto primero; activas e inactivas."""
    warehouses = visible_warehouses(membership=request.membership, include_inactive=True)
    page = Paginator(warehouses.order_by("-is_active", "-is_default", "name"), PAGE_SIZE).get_page(
        request.GET.get("page")
    )
    return render(
        request,
        "inventory/warehouse_list.html",
        {"page": page, "can_manage": has_perm(request.membership, Perm.INVENTORY_MANAGE)},
    )


@membership_required(perm=Perm.INVENTORY_MANAGE)
def warehouse_create_view(request):
    form = WarehouseForm(request.POST or None, membership=request.membership)
    if request.method == "POST" and form.is_valid():
        try:
            warehouse = services.create_warehouse(membership=request.membership, **form.cleaned_data)
        except ValidationError as error:
            apply_errors(form, error)
        else:
            messages.success(request, _("Bodega %(code)s creada.") % {"code": warehouse.code})
            return redirect("inventory_warehouse_list")
    return render(request, "inventory/warehouse_form.html", {"form": form, "warehouse": None})


@membership_required(perm=Perm.INVENTORY_MANAGE)
def warehouse_edit_view(request, pk):
    warehouse = _get_warehouse(request.membership, pk)
    form = WarehouseForm.for_warehouse(warehouse, membership=request.membership, data=request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            services.update_warehouse(membership=request.membership, warehouse=warehouse, **form.cleaned_data)
        except ValidationError as error:
            apply_errors(form, error)
        else:
            messages.success(request, _("Cambios guardados."))
            return redirect("inventory_warehouse_list")
    return render(request, "inventory/warehouse_form.html", {"form": form, "warehouse": warehouse})


@require_POST
@membership_required(perm=Perm.INVENTORY_MANAGE)
def warehouse_default_view(request, pk):
    warehouse = _get_warehouse(request.membership, pk)
    try:
        services.set_default_warehouse(membership=request.membership, warehouse=warehouse)
    except ValidationError as error:
        messages.error(request, " ".join(error.messages))
    else:
        messages.success(request, _("%(code)s es ahora la bodega por defecto.") % {"code": warehouse.code})
    return redirect("inventory_warehouse_list")


@require_POST
@membership_required(perm=Perm.INVENTORY_MANAGE)
def warehouse_active_view(request, pk):
    """Desactiva o reactiva; si no se puede, el mensaje explica por qué (INV-03 CA3)."""
    warehouse = _get_warehouse(request.membership, pk)
    is_active = request.POST.get("is_active") == "1"
    try:
        services.set_warehouse_active(membership=request.membership, warehouse=warehouse, is_active=is_active)
    except ValidationError as error:
        messages.error(request, " ".join(error.messages))
    else:
        text = _("Bodega %(code)s reactivada.") if is_active else _("Bodega %(code)s desactivada.")
        messages.success(request, text % {"code": warehouse.code})
    return redirect("inventory_warehouse_list")
