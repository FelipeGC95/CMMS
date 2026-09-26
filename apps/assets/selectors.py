from django.db.models import Count, Q

from apps.accounts.scoping import allowed_location_ids
from apps.organizations.selectors import visible_locations
from apps.work.models import FailureEvent

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


def asset_list(*, membership, search: str = "", status: str = "", parent=None):
    qs = (
        visible_assets(membership=membership).filter(is_active=True).select_related("asset_class", "location")
    )
    if parent is not None:
        qs = qs.filter(parent=parent)
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


def asset_children(*, membership, asset):
    """Componentes directos del activo (AST-03) que la membresía puede ver."""
    return asset_list(membership=membership, parent=asset)


def asset_status_history(asset):
    """Cambios de estado del activo, del más reciente al más antiguo (AST-02 CA2)."""
    return asset.status_periods.select_related("changed_by").order_by("-started_at")


def asset_failure_history(asset):
    """Fallas del activo o en las que fue el componente afectado (AST-02 CA2)."""
    return (
        FailureEvent.objects.filter(Q(asset=asset) | Q(component=asset))
        .select_related("asset", "component", "failure_mode", "work_order")
        .order_by("-started_at")
    )


def asset_form_choices(*, membership):
    """Opciones del formulario de activo: clases activas, ubicaciones y padres dentro del alcance."""
    return {
        "classes": AssetClass.objects.filter(is_active=True).order_by("name"),
        "locations": visible_locations(membership=membership).filter(is_active=True).order_by("name"),
        "parents": visible_assets(membership=membership).filter(is_active=True).order_by("code"),
    }
