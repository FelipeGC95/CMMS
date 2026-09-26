from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404

from apps.core.decorators import membership_required

from .models import Attachment, TargetType


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
    if target_type == TargetType.ASSET:
        from apps.assets.selectors import visible_assets

        return visible_assets(membership=membership).filter(pk=target_id).exists()
    return False


@membership_required
def attachment_view(request, pk):
    """Los archivos nunca se sirven públicamente: se verifica la organización y el alcance por sitio."""
    attachment = get_object_or_404(Attachment, pk=pk)
    if not _target_is_visible(
        target_type=attachment.target_type, target_id=attachment.target_id, membership=request.membership
    ):
        raise Http404
    return FileResponse(
        attachment.file.open("rb"), content_type=attachment.content_type, filename=attachment.filename
    )
