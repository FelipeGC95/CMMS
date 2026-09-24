from django.db import transaction
from django.utils import timezone

from .models import Asset, AssetStatusPeriod


@transaction.atomic
def change_asset_status(*, asset: Asset, status: str, by=None, reason: str = "", at=None) -> Asset:
    """Cierra el período de estado vigente y abre uno nuevo."""
    if asset.status == status:
        return asset
    at = at or timezone.now()
    AssetStatusPeriod.objects.filter(asset=asset, ended_at__isnull=True).update(ended_at=at)
    AssetStatusPeriod.objects.create(
        asset=asset, status=status, started_at=at, reason=reason[:200], changed_by=by
    )
    asset.status = status
    asset.save(update_fields=["status", "updated_at"])
    return asset
