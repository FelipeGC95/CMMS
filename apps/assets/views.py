import segno
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.safestring import mark_safe

from apps.accounts.permissions import Perm, PermissionDeniedError
from apps.core.decorators import membership_required
from apps.core.middleware import SESSION_KEY
from apps.work.forms import FailureReportForm
from apps.work.selectors import work_order_list
from apps.work.services import WorkOrderError, report_failure

from .models import Asset
from .selectors import asset_by_qr_token, asset_list


def _qr_svg(request, asset, scale=4):
    url = request.build_absolute_uri(reverse("asset_qr", args=[asset.qr_token]))
    svg = segno.make(url, error="m").svg_inline(scale=scale, dark="#0B2E4F", light="#FFFFFF")
    return mark_safe(svg)  # SVG generado por segno a partir de una URL nuestra.


@membership_required
def asset_list_view(request):
    search = request.GET.get("q", "").strip()
    status = request.GET.get("status", "")
    page = Paginator(asset_list(search=search, status=status), 25).get_page(request.GET.get("page"))
    return render(
        request,
        "assets/list.html",
        {"page": page, "search": search, "status": status, "statuses": Asset.Status.choices},
    )


@membership_required
def asset_detail_view(request, pk):
    asset = get_object_or_404(Asset.objects.select_related("asset_class", "location", "parent"), pk=pk)
    return render(
        request,
        "assets/detail.html",
        {
            "asset": asset,
            "qr_svg": _qr_svg(request, asset),
            "open_work_orders": work_order_list(scope="open", asset=asset),
            "history": work_order_list(scope="done", asset=asset)[:10],
            "plans": asset.plan_assignments.filter(is_active=True).select_related("plan"),
            "status_periods": asset.status_periods.all()[:10],
        },
    )


@membership_required
def asset_labels_view(request):
    assets = asset_list()
    labels = [(asset, _qr_svg(request, asset, scale=3)) for asset in assets]
    return render(request, "assets/labels.html", {"labels": labels})


@login_required
def asset_qr_view(request, token):
    """Destino del QR: con sesión va directo a avisar falla (AST-05).

    Si el activo es de otra organización del mismo usuario, cambia a esa
    organización; si el usuario no pertenece a ella, 404 (no se revela nada).
    """
    asset = asset_by_qr_token(token) if request.membership else None
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
        request.session[SESSION_KEY] = str(org_id)
        return redirect("asset_qr", token=token)
    return redirect("asset_report", pk=asset.pk)


@membership_required(perm=Perm.REQUEST_CREATE)
def asset_report_view(request, pk):
    asset = get_object_or_404(Asset, pk=pk, is_active=True)
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
            messages.success(request, f"Aviso enviado. Se creó la {work_request.work_order.code}.")
            return redirect("work_order_detail", pk=work_request.work_order_id)
    return render(request, "assets/report.html", {"asset": asset, "form": form})
