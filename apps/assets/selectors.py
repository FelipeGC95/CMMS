from django.db.models import Count, Q

from apps.accounts.scoping import allowed_location_ids
from apps.organizations.selectors import visible_locations
from apps.work.models import FailureEvent

from .models import Asset, AssetClass, AssetStatusPeriod


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


def label_assets(*, membership, ids=None, search: str = "", status: str = "", parent=None):
    """Activos para las etiquetas QR (AST-05 CA2): los marcados (`ids`) o los del filtro.

    Mismo alcance y filtros que la lista; solo trae lo que se imprime, ordenado por código.
    """
    qs = asset_list(membership=membership, search=search, status=status, parent=parent)
    if ids is not None:
        qs = qs.filter(pk__in=ids)
    return qs.select_related(None).only("id", "code", "name", "qr_token").order_by("code")


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


# Límite de niveles al recorrer la jerarquía (igual que la validación de ciclos).
MAX_HIERARCHY_DEPTH = 100


def asset_scope_ids(*, membership, asset, include_components: bool = False) -> list:
    """Ids del activo y, con `include_components`, de todos sus descendientes visibles (AST-03 CA2).

    Recorre la jerarquía por niveles: una consulta por nivel, sin importar
    cuántos componentes tenga cada uno. Solo devuelve descendientes dentro del
    alcance por sitio de la membresía (ORG-06).
    """
    if not include_components:
        return [asset.pk]
    found, frontier, depth = set(), [asset.pk], 0
    while frontier and depth < MAX_HIERARCHY_DEPTH:
        children = set(Asset.objects.filter(parent_id__in=frontier).values_list("id", flat=True)) - found
        children.discard(asset.pk)
        found |= children
        frontier, depth = list(children), depth + 1
    visible = set(visible_assets(membership=membership).filter(pk__in=found).values_list("id", flat=True))
    return [asset.pk, *sorted(visible)] if found else [asset.pk]


def asset_status_history(asset, *, asset_ids=None):
    """Cambios de estado del activo (o de `asset_ids`), del más reciente al más antiguo (AST-02 CA2)."""
    return (
        AssetStatusPeriod.objects.filter(asset_id__in=asset_ids or [asset.pk])
        .select_related("asset", "changed_by", "work_order")
        .order_by("-started_at")
    )


def asset_failure_history(asset, *, asset_ids=None):
    """Fallas del activo o en las que fue el componente afectado (AST-02 CA2).

    Con `asset_ids` (activo + componentes) incluye las de todos ellos.
    """
    ids = asset_ids or [asset.pk]
    return (
        FailureEvent.objects.filter(Q(asset_id__in=ids) | Q(component_id__in=ids))
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
