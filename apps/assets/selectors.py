from django.db.models import Count, Q

from apps.accounts.scoping import allowed_location_ids

from .models import Asset, AssetClass


def visible_assets(*, membership):
    """Activos que la membresía puede ver (ORG-06): todos, o solo los de sus ubicaciones.

    Toda búsqueda de un activo desde una vista pasa por aquí.
    """
    qs = Asset.objects.all()
    location_ids = allowed_location_ids(membership)
    if location_ids is not None:
        qs = qs.filter(location_id__in=location_ids)
    return qs


def asset_list(*, membership, search: str = "", status: str = ""):
    qs = (
        visible_assets(membership=membership).filter(is_active=True).select_related("asset_class", "location")
    )
    if search:
        qs = qs.filter(Q(code__icontains=search) | Q(name__icontains=search))
    if status:
        qs = qs.filter(status=status)
    return qs


def asset_by_qr_token(token: str, *, membership) -> Asset | None:
    return visible_assets(membership=membership).filter(qr_token=token, is_active=True).first()


def asset_class_list():
    """Clases de la organización con sus cantidades de activos y atributos visibles."""
    return AssetClass.objects.annotate(
        asset_count=Count("assets", filter=Q(assets__is_active=True), distinct=True),
        attribute_count=Count("attributes", filter=Q(attributes__is_active=True), distinct=True),
    ).order_by("-is_active", "name")


def active_attributes(asset_class):
    return list(asset_class.attributes.filter(is_active=True).order_by("order", "label"))
