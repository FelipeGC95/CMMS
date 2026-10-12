import uuid

import segno
from allauth.account.adapter import get_adapter
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.http import urlencode
from django.utils.safestring import mark_safe
from django.utils.translation import gettext as _
from django.utils.translation import ngettext
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST

from apps.accounts.antibot import consume_rate
from apps.accounts.permissions import Perm, PermissionDeniedError, has_perm
from apps.attachments.models import TargetType
from apps.attachments.selectors import attachments_for
from apps.attachments.services import AttachmentError
from apps.attachments.services import add_attachment as add_generic_attachment
from apps.condition.selectors import asset_condition
from apps.core.decorators import membership_required
from apps.core.files import private_file_response
from apps.core.middleware import SESSION_KEY
from apps.core.tenancy import tenant_context
from apps.maintenance.selectors import active_assignments_for_asset, reactivatable_assignments_for_asset
from apps.organizations.modules import has_module
from apps.organizations.services import quota_warning
from apps.reporting.selectors import asset_kpis
from apps.work.forms import AnonymousReportForm, FailureReportForm
from apps.work.selectors import work_order_list
from apps.work.services import WorkOrderError, report_anonymous_failure, report_failure

from .attributes import attribute_rows, missing_required
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
    asset_for_anonymous_report,
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
    """Búsqueda, estado, activo padre y "datos técnicos incompletos" de la lista, leídos de la URL.

    Un padre ajeno o inválido es 404.
    """
    search = request.GET.get("q", "").strip()
    status = request.GET.get("status", "")
    incomplete = request.GET.get("incomplete") == "1"
    parent = None
    if request.GET.get("parent"):
        try:
            parent = visible_assets(membership=request.membership).filter(pk=request.GET["parent"]).first()
        except ValidationError:
            parent = None
        if parent is None:
            raise Http404
    return search, status, parent, incomplete


@membership_required
def asset_list_view(request):
    search, status, parent, incomplete = _list_filters(request)
    assets = asset_list(
        membership=request.membership, search=search, status=status, parent=parent, incomplete=incomplete
    )
    page = Paginator(assets, 25).get_page(request.GET.get("page"))
    # Modo etiquetas (AST-05 CA2): casillas por fila y el formulario del PDF. Las marcas
    # se guardan en la sesión, así que valen entre páginas y búsquedas.
    labels_mode = request.GET.get("labels") == "1" and not request.GET.get("report")
    selected = set()
    if labels_mode:
        selected = {
            str(pk)
            for pk in label_assets(membership=request.membership, ids=_label_selection(request)).values_list(
                "pk", flat=True
            )
        }
    return render(
        request,
        "assets/list.html",
        {
            "page": page,
            "search": search,
            "status": status,
            "statuses": Asset.Status.choices,
            "parent": parent,
            "incomplete": incomplete,
            "can_manage": has_perm(request.membership, Perm.ASSET_MANAGE),
            "labels_mode": labels_mode,
            "label_layouts": [(key, layout.label) for key, layout in LAYOUTS.items()],
            "default_layout": DEFAULT_LAYOUT,
            "max_labels": MAX_LABELS,
            "labels_over_cap": labels_mode and page.paginator.count > MAX_LABELS,
            "selected_ids": selected,
            "selected_count": len(selected),
            "next_url": request.get_full_path(),
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
    attributes = active_attributes(asset.asset_class)
    can_manage = has_perm(membership, Perm.ASSET_MANAGE)
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
            **_meter_sections(request, asset),
            "can_read_meters": has_perm(membership, Perm.METER_READING_CREATE),
            "can_manage": can_manage,
            "can_manage_plans": has_perm(membership, Perm.PLAN_MANAGE),
            "attribute_rows": attribute_rows(asset, attributes),
            "missing_required_count": len(missing_required(asset, attributes)),
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


def _meter_sections(request, asset) -> dict:
    """Medidores y variables de la ficha (AST-07, CBM-01, CBM-07).

    Con el módulo `condition` y `condition.view`, las variables van en la sección "Condición"
    (estado, tendencia y alertas) y "Medidores" muestra solo los de uso. Sin ellos, las
    variables siguen siendo medidores: se leen en "Medidores" (CBM-08 CA2). Las desactivadas
    solo las ve quien puede reactivarlas.
    """
    membership = request.membership
    show_condition = has_module(request.organization, "condition") and has_perm(
        membership, Perm.CONDITION_VIEW
    )
    can_manage = has_perm(membership, Perm.ASSET_MANAGE)
    condition = asset_condition(membership=membership, asset=asset) if show_condition else []
    meters = [
        meter
        for meter in asset.meters.all()
        if (meter.is_active or (can_manage and meter.is_variable))
        and not (show_condition and meter.is_variable)
    ]
    meters.sort(key=lambda m: (not m.is_active, m.is_variable))
    for row in condition:
        row.form = MeterReadingForm(prefix=str(row.meter.pk), meter=row.meter)
    return {
        "meters": [(meter, MeterReadingForm(prefix=str(meter.pk), meter=meter)) for meter in meters],
        "show_condition": show_condition,
        "condition_rows": [row for row in condition if row.meter.is_active],
        "inactive_variables": [row for row in condition if not row.meter.is_active],
    }


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
    if request.method == "GET" and (
        warning := quota_warning(organization=request.organization, resource="assets")
    ):
        messages.warning(request, warning)  # ORG-10 CA1: aviso al 80 % del cupo
    if request.method == "POST" and form.is_valid():
        try:
            asset = create_asset(membership=membership, **form.service_kwargs())
        except PermissionDeniedError as exc:
            raise PermissionDenied from exc
        except ValidationError as error:
            form.add_error(None, error)
        else:
            messages.success(request, _("Activo %(code)s creado.") % {"code": asset.code})
            plans = asset.plan_assignments.filter(is_active=True).count()
            if plans:
                messages.info(
                    request,
                    ngettext(
                        "Se asignó %(n)s plan preventivo de su clase.",
                        "Se asignaron %(n)s planes preventivos de su clase.",
                        plans,
                    )
                    % {"n": plans},
                )
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
    """Lectura manual de un medidor o de una variable (AST-07, CBM-02 CA1)."""
    meter = get_object_or_404(
        Meter.objects.select_related("asset").filter(asset__in=visible_assets(membership=request.membership)),
        pk=meter_pk,
        asset_id=pk,
        is_active=True,
    )
    back = f"{reverse('asset_detail', args=[pk])}#meter-{meter.pk}"
    form = MeterReadingForm(request.POST, prefix=str(meter.pk), meter=meter)
    if not form.is_valid():
        if meter.is_variable:
            messages.error(request, _("Ingresa una lectura válida (un número)."))
        else:
            messages.error(request, _("Ingresa una lectura válida (número mayor o igual a cero)."))
        return redirect(back)
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
        label = meter.name or meter.get_kind_display()
        messages.success(request, _("Lectura registrada: %(kind)s.") % {"kind": label})
        for warning in warnings:
            messages.warning(request, warning)
    return redirect(back)


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


def _selection_key(request):
    return f"label_selection:{request.organization.pk}"


def _label_selection(request):
    """Ids marcados para etiquetas, guardados en la sesión por organización."""
    return _selected_ids(request.session.get(_selection_key(request), []))


def _safe_next(request):
    next_url = request.POST.get("next", "")
    if next_url.startswith(reverse("asset_list")):
        return next_url
    return f"{reverse('asset_list')}?labels=1"


@require_POST
@membership_required
def asset_labels_select_view(request):
    """Guarda las marcas de la página en la selección de etiquetas (AST-05 CA2).

    `page_ids` son los activos que se veían y `ids` los marcados entre ellos: se
    reemplaza esa parte de la selección y se conserva la de otras páginas. Con htmx
    se llama en cada casilla y devuelve el contador; sin JS, los botones del
    formulario guardan y luego imprimen o vuelven a la lista.
    """
    action = request.POST.get("action", "save")
    selection = [] if action == "clear" else _label_selection(request)
    if action != "clear":
        page_ids = set(_selected_ids(request.POST.getlist("page_ids")))
        marked = [
            pk
            for pk in label_assets(
                membership=request.membership, ids=_selected_ids(request.POST.getlist("ids"))
            ).values_list("pk", flat=True)
            if pk in page_ids
        ]
        selection = [pk for pk in selection if pk not in page_ids]
        selection += [pk for pk in marked if pk not in selection]
        if len(selection) > MAX_LABELS:
            selection = selection[:MAX_LABELS]
            messages.warning(
                request,
                _("Un PDF admite hasta %(max)s etiquetas; se guardaron las primeras %(max)s marcas.")
                % {"max": MAX_LABELS},
            )
    request.session[_selection_key(request)] = [str(pk) for pk in selection]
    if request.headers.get("HX-Request"):
        return render(request, "assets/_label_selection_count.html", {"selected_count": len(selection)})
    if action in ("print_selected", "print_filtered"):
        params = {"size": request.POST.get("size", DEFAULT_LAYOUT)}
        if action == "print_selected":
            params["scope"] = "selected"
        else:
            params["scope"] = "filtered"
            params.update(
                {k: request.POST[k] for k in ("q", "status", "parent", "incomplete") if request.POST.get(k)}
            )
        return redirect(f"{reverse('asset_labels_pdf')}?{urlencode(params)}")
    if action == "clear":
        messages.info(request, _("Selección de etiquetas vaciada."))
    return redirect(_safe_next(request))


@membership_required
def asset_labels_pdf_view(request):
    """PDF de etiquetas QR, varias por hoja A4 (AST-05 CA2).

    `scope=selected` imprime los activos marcados (`ids` o, sin ellos, la selección
    guardada en la sesión); si no, todos los del filtro actual. Siempre dentro del
    alcance de la membresía y con un tope de `MAX_LABELS`.
    """
    search, status, parent, incomplete = _list_filters(request)
    filters = {
        "q": search,
        "status": status,
        "parent": parent.pk if parent else "",
        "incomplete": "1" if incomplete else "",
    }
    back = f"{reverse('asset_list')}?{urlencode({**{k: v for k, v in filters.items() if v}, 'labels': 1})}"
    ids = None
    if request.GET.get("scope") == "selected":
        ids = _selected_ids(request.GET.getlist("ids")) or _label_selection(request)
        if not ids:
            messages.warning(request, _("Marca al menos un activo para imprimir sus etiquetas."))
            return redirect(back)
    assets = label_assets(
        membership=request.membership,
        ids=ids,
        search=search,
        status=status,
        parent=parent,
        incomplete=incomplete,
    )
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


# Aviso por QR sin cuenta (REQ-02 CA2): límites por IP y por activo, configurables.
QR_REPORT_LIMIT_PER_IP = (5, 60 * 60)  # 5 envíos por hora desde una IP
QR_REPORT_LIMIT_PER_ASSET = (10, 24 * 60 * 60)  # 10 avisos al día por activo


def _qr_limit(name, default):
    return getattr(settings, name, default)


def asset_qr_view(request, token):
    """Destino del QR: con sesión va directo a avisar falla (AST-05 CA3).

    Si el activo es de otra organización del mismo usuario, cambia a esa
    organización; si el usuario no pertenece a ella, 404 (no se revela nada).
    Sin sesión, muestra el aviso sin cuenta si la organización lo permite
    (REQ-02); si no, pide ingresar.
    """
    if not request.user.is_authenticated:
        return _anonymous_report(request, token)
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


@never_cache
def _anonymous_report(request, token):
    """Formulario público mínimo (REQ-02 CA1): solo muestra el activo y la organización del QR.

    El contexto de organización sale únicamente del activo del token. Si la
    organización no lo permite (o el token no existe), el mismo resultado que antes:
    pedir ingresar, sin revelar si el activo existe.
    """
    asset = asset_for_anonymous_report(token)
    if asset is None:
        return redirect_to_login(request.get_full_path())
    form = AnonymousReportForm(request.POST or None, request.FILES or None)
    if request.method == "POST":
        ip = get_adapter().get_client_ip(request)
        limit, period = _qr_limit("QR_REPORT_LIMIT_PER_IP", QR_REPORT_LIMIT_PER_IP)
        if not consume_rate(f"qr-ip:{ip}", limit=limit, period=period):
            return _anonymous_limited(request, asset)
        if form.is_valid():
            limit, period = _qr_limit("QR_REPORT_LIMIT_PER_ASSET", QR_REPORT_LIMIT_PER_ASSET)
            if not consume_rate(f"qr-asset:{asset.pk}", limit=limit, period=period):
                return _anonymous_limited(request, asset)
            data = form.cleaned_data
            with tenant_context(asset.organization):
                try:
                    report_anonymous_failure(
                        asset=asset,
                        name=data["name"],
                        description=data["description"],
                        asset_stopped=data["asset_stopped"],
                        photo=data.get("photo"),
                    )
                except WorkOrderError as exc:
                    form.add_error("photo", exc.message)
                else:
                    return redirect("asset_qr_sent", token=token)
    return render(
        request,
        "assets/public_report.html",
        {"asset": asset, "organization": asset.organization, "form": form},
    )


def _anonymous_limited(request, asset):
    return render(
        request,
        "assets/public_report_limited.html",
        {"asset": asset, "organization": asset.organization},
        status=429,
    )


@never_cache
def asset_qr_sent_view(request, token):
    """Confirmación del aviso sin cuenta: no muestra el número ni datos de la solicitud."""
    asset = asset_for_anonymous_report(token)
    if asset is None:
        raise Http404
    return render(
        request, "assets/public_report_sent.html", {"asset": asset, "organization": asset.organization}
    )


@never_cache
def asset_qr_logo_view(request, token):
    """Logo de la organización para el aviso sin cuenta; solo con un QR válido y el aviso activo."""
    asset = asset_for_anonymous_report(token)
    if asset is None or not asset.organization.logo:
        raise Http404
    return private_file_response(asset.organization.logo, content_type="image/png")


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
