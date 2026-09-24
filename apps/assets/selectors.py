from django.db.models import Q

from .models import Asset


def asset_list(*, search: str = "", status: str = ""):
    qs = Asset.objects.filter(is_active=True).select_related("asset_class", "location")
    if search:
        qs = qs.filter(Q(code__icontains=search) | Q(name__icontains=search))
    if status:
        qs = qs.filter(status=status)
    return qs


def asset_by_qr_token(token: str) -> Asset | None:
    return Asset.objects.filter(qr_token=token, is_active=True).first()
