from django.db.models import CharField, Count, Exists, F, Func, OuterRef, Q, Value
from django.db.models.functions import Coalesce

from apps.accounts.scoping import allowed_location_ids
from apps.organizations.selectors import visible_locations
from apps.work.models import FailureEvent

from .models import Asset, AssetClass, AssetClassAttribute, AssetStatusPeriod


class JSONKeyText(Func):
    """Texto del valor de `json_field` en la clave dada por otra columna (clave dinámica).

    Django solo admite claves JSON fijas; aquí la clave es la del atributo de
    la clase. Las claves son slugs, así que es seguro armar la ruta en SQLite.
    """

    arity = 2
    output_field = CharField()

    def _compile(self, compiler):
        (json_sql, json_params), (key_sql, key_params) = (
            compiler.compile(expression) for expression in self.get_source_expressions()
        )
        return json_sql, key_sql, (*json_params, *key_params)

    def as_sql(self, compiler, connection, **extra_context):  # PostgreSQL (jsonb)
        json_sql, key_sql, params = self._compile(compiler)
        return f"({json_sql} ->> {key_sql})", params

    def as_sqlite(self, compiler, connection, **extra_context):
        json_sql, key_sql, params = self._compile(compiler)
        return f"json_extract({json_sql}, '$.\"' || {key_sql} || '\"')", params


def missing_required_attributes():
    """Atributos obligatorios y activos de la clase del activo exterior sin valor (AST-01).

    Para usar en `Exists(...)` sobre una consulta de activos: sin valor es
    clave ausente, nula o vacía. Los atributos desactivados no cuentan.
    """
    value = Coalesce(JSONKeyText(OuterRef("attributes"), F("key")), Value(""))
    return (
        AssetClassAttribute.objects.filter(
            asset_class=OuterRef("asset_class_id"), required=True, is_active=True
        )
        .annotate(value=value)
        .filter(value="")
    )


def visible_assets(*, membership):
    """Activos que la membresía puede ver (ORG-06): todos, o solo los de sus ubicaciones.

    Toda búsqueda de un activo desde una vista pasa por aquí.
    """
    qs = Asset.objects.all()
    location_ids = allowed_location_ids(membership)
    if location_ids is not None:
        qs = qs.filter(location_id__in=location_ids)
    return qs


def asset_list(*, membership, search: str = "", status: str = "", parent=None, incomplete: bool = False):
    """Activos visibles; `incomplete`: solo los que no tienen algún dato técnico obligatorio."""
    qs = (
        visible_assets(membership=membership).filter(is_active=True).select_related("asset_class", "location")
    )
    if parent is not None:
        qs = qs.filter(parent=parent)
    if search:
        qs = qs.filter(Q(code__icontains=search) | Q(name__icontains=search))
    if status:
        qs = qs.filter(status=status)
    if incomplete:
        qs = qs.filter(Exists(missing_required_attributes()))
    return qs


def label_assets(
    *, membership, ids=None, search: str = "", status: str = "", parent=None, incomplete: bool = False
):
    """Activos para las etiquetas QR (AST-05 CA2): los marcados (`ids`) o los del filtro.

    Mismo alcance y filtros que la lista; solo trae lo que se imprime, ordenado por código.
    """
    qs = asset_list(membership=membership, search=search, status=status, parent=parent, incomplete=incomplete)
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
