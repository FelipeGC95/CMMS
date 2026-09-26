import uuid

import segno
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.http import urlencode
from django.utils.safestring import mark_safe
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from apps.accounts.permissions import Perm, PermissionDeniedError, has_perm
from apps.attachments.models import TargetType
from apps.attachments.selectors import attachments_for
from apps.attachments.services import AttachmentError
from apps.attachments.services import add_attachment as add_generic_attachment
from apps.core.decorators import membership_required
from apps.core.middleware import SESSION_KEY
from apps.maintenance.selectors import active_assignments_for_asset, reactivatable_assignments_for_asset
from apps.reporting.selectors import asset_kpis
from apps.work.forms import FailureReportForm
from apps.work.selectors import work_order_list
from apps.work.services import WorkOrderError, report_failure

from .attributes import attribute_rows
from .forms import (
    AssetAttachmentForm,
    AssetDecommissionForm,
    AssetForm,
    AssetReactivateForm,
    AssetStatusForm,
    MeterReadingForm,
)
from .labels import DEFAULT_LAYOUT, LAYOUTS, MAX_LABELS, render_labels_pdf
from .models import Asset, Meter
from .selectors import (
    active_attributes,
    asset_by_qr_token,
    asset_children,
    asset_failure_history,
    asset_list,
    asset_scope_ids,
    asset_status_history,
    label_assets,
    visible_assets,
)
from .services import (
    create_asset,
    decommission_asset,
    reactivate_asset,
    record_meter_reading,
    set_asset_status,
    update_asset,
)

# La ficha muestra los últimos registros de cada historial; el resto, en su lista.
HISTORY_LIMIT = 10


def _qr_svg(request, asset, scale=4):
    url = request.build_absolute_uri(reverse("asset_qr", args=[asset.qr_token]))
    svg = segno.make(url, error="m").svg_inline(scale=scale, dark="#0B2E4F", light="#FFFFFF")
    return mark_safe(svg)  # SVG generado por segno a partir de una URL nuestra.


def _list_filters(request):
    """Búsqueda, estado y activo padre de la lista, leídos de la URL (padre ajeno o inválido: 404)."""
    search = request.GET.get("q", "").strip()
    status = request.GET.get("status", "")
    parent = None
    if request.GET.get("parent"):
        try:
            parent = visible_assets(membership=request.membership).filter(pk=request.GET["parent"]).first()
        except ValidationError:
            parent = None
        if parent is None:
            raise Http404
    return search, status, parent


@membership_required
def asset_list_view(request):
    search, status, parent = _list_filters(request)
    assets = asset_list(membership=request.membership, search=search, status=status, parent=parent)
    page = Paginator(assets, 25).get_page(request.GET.get("page"))
    # Modo etiquetas (AST-05 CA2): casillas por fila y el formulario del PDF.
    labels_mode = request.GET.get("labels") == "1" and not request.GET.get("report")
    return render(
        request,
        "assets/list.html",
        {
            "page": page,
            "search": search,
            "status": status,
            "statuses": Asset.Status.choices,
            "parent": parent,
            "can_manage": has_perm(request.membership, Perm.ASSET_MANAGE),
            "labels_mode": labels_mode,
            "label_layouts": [(key, layout.label) for key, layout in LAYOUTS.items()],
            "default_layout": DEFAULT_LAYOUT,
            "max_labels": MAX_LABELS,
            "labels_over_cap": labels_mode and page.paginator.count > MAX_LABELS,
        },
    )


@membership_required
def asset_detail_view(request, pk):
    membership = request.membership
    asset = get_object_or_404(
        visible_assets(membership=membership).select_related("asset_class", "location", "parent"),
        pk=pk,
    )
    children = asset_children(membership=membership, asset=asset)
    children_count = children.count()
    # "Incluir componentes" (AST-03 CA2): historial y KPI del activo y sus descendientes.
    include_components = request.GET.get("components") == "1" and children_count > 0
    ids = asset_scope_ids(membership=membership, asset=asset, include_components=include_components)
    done = work_order_list(membership=membership, scope="done").filter(asset_id__in=ids)
    return render(
        request,
        "assets/detail.html",
        {
            "asset": asset,
            "is_retired": asset.status == Asset.Status.RETIRED,
            "qr_svg": _qr_svg(request, asset),
            "open_work_orders": work_order_list(membership=membership, scope="open", asset=asset),
            "history": done[:HISTORY_LIMIT],
            "plans": active_assignments_for_asset(asset=asset),
            "meters": [
                (meter, MeterReadingForm(prefix=str(meter.pk)))
                for meter in asset.meters.filter(is_active=True)
            ],
            "can_read_meters": has_perm(membership, Perm.METER_READING_CREATE),
            "can_manage": has_perm(membership, Perm.ASSET_MANAGE),
            "attribute_rows": attribute_rows(asset, active_attributes(asset.asset_class)),
            "include_components": include_components,
            "kpis": asset_kpis(membership=membership, asset=asset, include_components=include_components),
            "status_periods": asset_status_history(asset, asset_ids=ids)[:HISTORY_LIMIT],
            "failures": asset_failure_history(asset, asset_ids=ids)[:HISTORY_LIMIT],
            "children": children[:HISTORY_LIMIT],
            "children_count": children_count,
            "history_limit": HISTORY_LIMIT,
            "attachments": attachments_for(target_type=TargetType.ASSET, target_id=asset.pk),
            "attachment_form": AssetAttachmentForm(),
        },
    )


def _manageable_asset(request, pk):
    """Activo visible y no dado de baja, para las acciones de estado (AST-04)."""
    asset = get_object_or_404(
        visible_assets(membership=request.membership).select_related("location"), pk=pk, is_active=True
    )
    if asset.status == Asset.Status.RETIRED:
        messages.info(request, _("El activo está dado de baja."))
        return None, redirect("asset_detail", pk=asset.pk)
    return asset, None


@membership_required(perm=Perm.ASSET_MANAGE)
def asset_status_view(request, pk):
    """Cambio manual de estado con motivo (AST-04): abre y cierra períodos de estado."""
    asset, response = _manageable_asset(request, pk)
    if response:
        return response
    form = AssetStatusForm(request.POST or None, asset=asset)
    if request.method == "POST" and form.is_valid():
        try:
            set_asset_status(
                membership=request.membership,
                asset=asset,
                status=form.cleaned_data["status"],
                reason=form.cleaned_data["reason"],
            )
        except PermissionDeniedError as exc:
            raise PermissionDenied from exc
        except ValidationError as error:
            form.add_error(None, error)
        else:
            asset.refresh_from_db(fields=["status"])
            messages.success(
                request, _("Estado cambiado a %(status)s.") % {"status": asset.get_status_display()}
            )
            return redirect(f"{reverse('asset_detail', args=[asset.pk])}#status-history")
    return render(request, "assets/status_form.html", {"asset": asset, "form": form})


@membership_required(perm=Perm.ASSET_MANAGE)
def asset_decommission_view(request, pk):
    """Dar de baja (AST-04 CA3): confirma mostrando los planes que se desactivan y las OT abiertas."""
    asset, response = _manageable_asset(request, pk)
    if response:
        return response
    form = AssetDecommissionForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            decommission_asset(membership=request.membership, asset=asset, reason=form.cleaned_data["reason"])
        except PermissionDeniedError as exc:
            raise PermissionDenied from exc
        except ValidationError as error:
            form.add_error(None, error)
        else:
            messages.success(request, _("Activo %(code)s dado de baja.") % {"code": asset.code})
            return redirect("asset_detail", pk=asset.pk)
    return render(
        request,
        "assets/decommission.html",
        {
            "asset": asset,
            "form": form,
            "assignments": active_assignments_for_asset(asset=asset),
            "open_work_orders": work_order_list(membership=request.membership, scope="open", asset=asset),
            # Sin cascada: los componentes siguen operativos y se dan de baja por separado.
            "active_children": asset_children(membership=request.membership, asset=asset).exclude(
                status=Asset.Status.RETIRED
            ),
        },
    )


@membership_required(perm=Perm.ASSET_MANAGE)
def asset_reactivate_view(request, pk):
    """Reactivar un activo dado de baja (AST-04): motivo y planes elegidos para reactivar."""
    asset = get_object_or_404(
        visible_assets(membership=request.membership).select_related("location"), pk=pk, is_active=True
    )
    if asset.status != Asset.Status.RETIRED:
        messages.info(request, _("El activo no está dado de baja."))
        return redirect("asset_detail", pk=asset.pk)
    form = AssetReactivateForm(
        request.POST or None, assignments=reactivatable_assignments_for_asset(asset=asset)
    )
    if request.method == "POST" and form.is_valid():
        try:
            reactivate_asset(
                membership=request.membership,
                asset=asset,
                reason=form.cleaned_data["reason"],
                assignment_ids=form.assignment_ids(),
            )
        except PermissionDeniedError as exc:
            raise PermissionDenied from exc
        except ValidationError as error:
            form.add_error(None, error)
        else:
            messages.success(request, _("Activo %(code)s reactivado.") % {"code": asset.code})
            return redirect("asset_detail", pk=asset.pk)
    return render(request, "assets/reactivate.html", {"asset": asset, "form": form})


@membership_required(perm=Perm.ASSET_MANAGE)
def asset_create_view(request):
    """Alta de activo (AST-02 CA1/CA3). `?parent=<id>` precarga el padre para agregar un componente."""
    membership = request.membership
    initial = {}
    parent_id = request.GET.get("parent")
    if parent_id:
        try:
            parent = visible_assets(membership=membership).filter(pk=parent_id, is_active=True).first()
        except ValidationError:
            parent = None
        if parent is not None:
            initial = {"parent": parent.pk, "location": parent.location_id}
    form = AssetForm(request.POST or None, membership=membership, initial=initial)
    if request.method == "POST" and form.is_valid():
        try:
            asset = create_asset(membership=membership, **form.service_kwargs())
        except PermissionDeniedError as exc:
            raise PermissionDenied from exc
        except ValidationError as error:
            form.add_error(None, error)
        else:
            messages.success(request, _("Activo %(code)s creado.") % {"code": asset.code})
            return redirect("asset_detail", pk=asset.pk)
    return render(request, "assets/asset_form.html", {"form": form, "asset": None})


@membership_required(perm=Perm.ASSET_MANAGE)
def asset_edit_view(request, pk):
    membership = request.membership
    asset = get_object_or_404(
        visible_assets(membership=membership).select_related("asset_class"), pk=pk, is_active=True
    )
    form = AssetForm(request.POST or None, membership=membership, asset=asset)
    if request.method == "POST" and form.is_valid():
        try:
            update_asset(membership=membership, asset=asset, **form.service_kwargs())
        except PermissionDeniedError as exc:
            raise PermissionDenied from exc
        except ValidationError as error:
            form.add_error(None, error)
        else:
            messages.success(request, _("Cambios guardados."))
            return redirect("asset_detail", pk=asset.pk)
    return render(request, "assets/asset_form.html", {"form": form, "asset": asset})


@membership_required(perm=Perm.ASSET_MANAGE)
def asset_attribute_fields_view(request):
    """Fragmento htmx: campos de los atributos de la clase elegida en el formulario de activo."""
    membership = request.membership
    asset = None
    asset_id = request.GET.get("asset")
    if asset_id:
        try:
            asset = (
                visible_assets(membership=membership)
                .select_related("asset_class")
                .filter(pk=asset_id)
                .first()
            )
        except ValidationError:
            asset = None
    initial = {"asset_class": request.GET.get("asset_class", "")}
    form = AssetForm(membership=membership, asset=asset, initial=initial)
    return render(request, "assets/_asset_attribute_fields.html", {"form": form})


@require_POST
@membership_required(perm=Perm.ASSET_MANAGE)
def asset_attachment_add_view(request, pk):
    asset = get_object_or_404(visible_assets(membership=request.membership), pk=pk)
    form = AssetAttachmentForm(request.POST, request.FILES)
    if form.is_valid():
        try:
            add_generic_attachment(
                target_type=TargetType.ASSET,
                target_id=asset.pk,
                uploaded_file=form.cleaned_data["file"],
                by=request.user,
                purpose=form.cleaned_data["purpose"],
                client_uuid=form.cleaned_data.get("client_uuid"),
            )
        except AttachmentError as error:
            messages.error(request, error.message)
        else:
            messages.success(request, _("Adjunto guardado."))
    else:
        messages.error(request, _("Revisa el archivo: es obligatorio y debe pesar hasta 20 MB."))
    return redirect(f"{reverse('asset_detail', args=[pk])}#attachments")


@require_POST
@membership_required(perm=Perm.METER_READING_CREATE)
def meter_reading_view(request, pk, meter_pk):
    meter = get_object_or_404(
        Meter.objects.select_related("asset").filter(asset__in=visible_assets(membership=request.membership)),
        pk=meter_pk,
        asset_id=pk,
    )
    form = MeterReadingForm(request.POST, prefix=str(meter.pk))
    if not form.is_valid():
        messages.error(request, _("Ingresa una lectura válida (número mayor o igual a cero)."))
        return redirect(f"{reverse('asset_detail', args=[pk])}#meters")
    try:
        _reading, warnings = record_meter_reading(
            meter=meter,
            value=form.cleaned_data["value"],
            membership=request.membership,
            is_reset=form.cleaned_data["is_reset"],
            client_uuid=form.cleaned_data.get("client_uuid"),
        )
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
    else:
        messages.success(request, _("Lectura registrada: %(kind)s.") % {"kind": meter.get_kind_display()})
        for warning in warnings:
            messages.warning(request, warning)
    return redirect(f"{reverse('asset_detail', args=[pk])}#meters")


@membership_required
def asset_labels_view(request):
    """Dirección antigua de las etiquetas: ahora se eligen en la lista y se imprimen en PDF."""
    return redirect(f"{reverse('asset_list')}?labels=1")


def _selected_ids(values):
    ids = []
    for value in values:
        try:
            ids.append(uuid.UUID(value))
        except ValueError:
            continue  # un id malformado se ignora, como uno de otra organización
    return ids


@membership_required
def asset_labels_pdf_view(request):
    """PDF de etiquetas QR, varias por hoja A4 (AST-05 CA2).

    `scope=selected` imprime los activos marcados (`ids`); si no, todos los del filtro
    actual. Siempre dentro del alcance de la membresía y con un tope de `MAX_LABELS`.
    """
    search, status, parent = _list_filters(request)
    filters = {"q": search, "status": status, "parent": parent.pk if parent else ""}
    back = f"{reverse('asset_list')}?{urlencode({**{k: v for k, v in filters.items() if v}, 'labels': 1})}"
    ids = None
    if request.GET.get("scope") == "selected":
        ids = _selected_ids(request.GET.getlist("ids"))
        if not ids:
            messages.warning(request, _("Marca al menos un activo para imprimir sus etiquetas."))
            return redirect(back)
    assets = label_assets(membership=request.membership, ids=ids, search=search, status=status, parent=parent)
    total = assets.count()
    if total == 0:
        messages.info(request, _("No hay activos para imprimir."))
        return redirect(back)
    if total > MAX_LABELS:
        messages.error(
            request,
            _(
                "Son %(total)s activos y un PDF admite hasta %(max)s etiquetas. "
                "Filtra la lista (búsqueda, estado o activo padre) y genera varios PDF."
            )
            % {"total": total, "max": MAX_LABELS},
        )
        return redirect(back)
    pdf = render_labels_pdf(
        assets=assets,
        organization=request.organization,
        qr_url=lambda token: request.build_absolute_uri(reverse("asset_qr", args=[token])),
        layout_key=request.GET.get("size", DEFAULT_LAYOUT),
    )
    response = HttpResponse(pdf, content_type="application/pdf")
    response["Content-Disposition"] = f'inline; filename="etiquetas-qr-{request.organization.slug}.pdf"'
    return response


@login_required
def asset_qr_view(request, token):
    """Destino del QR: con sesión va directo a avisar falla (AST-05).

    Si el activo es de otra organización del mismo usuario, cambia a esa
    organización; si el usuario no pertenece a ella, 404 (no se revela nada).
    """
    asset = asset_by_qr_token(token, membership=request.membership) if request.membership else None
    if asset is None:
        # Búsqueda entre organizaciones justificada: solo se usa el organization_id
        # para comprobar que el usuario tiene membresía en él.
        org_id = (
            Asset.all_tenants.filter(qr_token=token, is_active=True)
            .values_list("organization_id", flat=True)
            .first()
        )
        if org_id is None or not any(m.organization_id == org_id for m in request.memberships):
            raise Http404
        # Es de esta organización pero fuera de su alcance por sitio (ORG-06): no se revela.
        if request.organization is not None and org_id == request.organization.pk:
            raise Http404
        request.session[SESSION_KEY] = str(org_id)
        return redirect("asset_qr", token=token)
    return redirect("asset_report", pk=asset.pk)


@membership_required(perm=Perm.REQUEST_CREATE)
def asset_report_view(request, pk):
    asset = get_object_or_404(visible_assets(membership=request.membership), pk=pk, is_active=True)
    form = FailureReportForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        try:
            work_request = report_failure(
                membership=request.membership,
                asset=asset,
                description=form.cleaned_data["description"],
                asset_stopped=form.cleaned_data["asset_stopped"],
                photo=form.cleaned_data.get("photo"),
                client_uuid=form.cleaned_data.get("client_uuid"),
            )
        except PermissionDeniedError as exc:
            raise PermissionDenied from exc
        except WorkOrderError as exc:
            form.add_error(None, exc.message)
        else:
            messages.success(
                request,
                _("Aviso enviado. Se creó la %(code)s.") % {"code": work_request.work_order.code},
            )
            if getattr(work_request, "photo_warning", ""):  # cupo lleno: el aviso va sin foto
                messages.warning(request, work_request.photo_warning)
            return redirect("work_order_detail", pk=work_request.work_order_id)
    return render(request, "assets/report.html", {"asset": asset, "form": form})
