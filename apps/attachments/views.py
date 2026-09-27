from django.contrib import messages
from django.core.paginator import Paginator
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from apps.accounts.permissions import Perm
from apps.core.decorators import membership_required
from apps.core.files import private_file_response

from . import selectors, services
from .forms import ManualForm, ManualSearchForm
from .models import Attachment, TargetType

SEARCH_PAGE_SIZE = 20


def _target_is_visible(*, target_type: str, target_id, membership) -> bool:
    """Cada app dueña del target decide su propio alcance (import local: evita ciclos)."""
    if target_type == TargetType.WORK_ORDER:
        from apps.work.selectors import visible_work_orders

        return visible_work_orders(membership=membership).filter(pk=target_id).exists()
    if target_type == TargetType.CHECKLIST_ITEM:
        from apps.work.models import WorkOrderChecklistItem
        from apps.work.selectors import visible_work_orders

        return WorkOrderChecklistItem.objects.filter(
            pk=target_id, work_order__in=visible_work_orders(membership=membership)
        ).exists()
    if target_type == TargetType.WORK_REQUEST:
        from apps.work.selectors import visible_requests

        return visible_requests(membership=membership).filter(pk=target_id).exists()
    if target_type == TargetType.ASSET:
        from apps.assets.selectors import visible_assets

        return visible_assets(membership=membership).filter(pk=target_id).exists()
    if target_type == TargetType.ASSET_CLASS:
        return selectors.visible_manuals(membership=membership).filter(target_id=target_id).exists()
    return False


@membership_required
def attachment_view(request, pk):
    """Los archivos nunca se sirven públicamente: se verifica la organización y el alcance por sitio."""
    attachment = get_object_or_404(Attachment, pk=pk)
    if not _target_is_visible(
        target_type=attachment.target_type, target_id=attachment.target_id, membership=request.membership
    ):
        raise Http404
    return private_file_response(
        attachment.file, content_type=attachment.content_type, filename=attachment.filename
    )


# --- Manuales (AST-06 CA3) ---------------------------------------------------------------


def _target_url(attachment) -> str:
    name = "asset_class_detail" if attachment.target_type == TargetType.ASSET_CLASS else "asset_detail"
    return reverse(name, args=[attachment.target_id]) + "#manuals"


def _add_manual(request, *, target_type, target_id) -> None:
    form = ManualForm(request.POST, request.FILES)
    if not form.is_valid():
        messages.error(request, _("Revisa el archivo: es obligatorio y debe ser un PDF de hasta 20 MB."))
        return
    try:
        services.add_manual(
            target_type=target_type,
            target_id=target_id,
            uploaded_file=form.cleaned_data["file"],
            by=request.user,
            client_uuid=form.cleaned_data.get("client_uuid"),
        )
    except services.AttachmentError as error:
        messages.error(request, error.message)
    else:
        messages.success(request, _("Manual guardado."))


@require_POST
@membership_required(perm=Perm.ASSET_MANAGE)
def asset_manual_add_view(request, pk):
    from apps.assets.selectors import visible_assets

    asset = get_object_or_404(visible_assets(membership=request.membership), pk=pk)
    _add_manual(request, target_type=TargetType.ASSET, target_id=asset.pk)
    return redirect(reverse("asset_detail", args=[asset.pk]) + "#manuals")


@require_POST
@membership_required(perm=Perm.ASSET_MANAGE)
def asset_class_manual_add_view(request, pk):
    from apps.assets.models import AssetClass

    asset_class = get_object_or_404(AssetClass, pk=pk)
    _add_manual(request, target_type=TargetType.ASSET_CLASS, target_id=asset_class.pk)
    return redirect(reverse("asset_class_detail", args=[asset_class.pk]) + "#manuals")


@membership_required(perm=Perm.ASSET_MANAGE)
def manual_remove_view(request, pk):
    """GET muestra la confirmación (sin JS); POST quita el manual."""
    manual = get_object_or_404(selectors.visible_manuals(membership=request.membership), pk=pk)
    back = _target_url(manual)
    if request.method != "POST":
        return render(request, "attachments/manual_remove_confirm.html", {"manual": manual, "back": back})
    services.remove_manual(attachment=manual, by=request.user)
    messages.success(request, _("Manual «%(name)s» quitado.") % {"name": manual.filename})
    return redirect(back)


@membership_required
def manual_search_view(request):
    """Búsqueda en el texto de los manuales visibles (AST-06 CA3), con alcance por sitio."""
    form = ManualSearchForm(request.GET or None)
    query = form.cleaned_data["q"].strip() if form.is_valid() else ""
    page = None
    if query:
        pages = selectors.search_manual_pages(membership=request.membership, query=query)
        page = Paginator(pages, SEARCH_PAGE_SIZE).get_page(request.GET.get("page"))
        terms = selectors.search_terms(query)
        targets = selectors.manual_targets(hit.attachment for hit in page)
        for hit in page:
            hit.snippet = selectors.snippet(hit.text, terms)
            hit.target = targets.get(hit.attachment.target_id)
    return render(
        request,
        "attachments/manual_search.html",
        {"form": form, "query": query, "page": page, "class_type": TargetType.ASSET_CLASS},
    )
