from dataclasses import dataclass

from django.db.models import Count

from apps.accounts.scoping import allowed_location_ids

from .models import Location


def visible_locations(*, membership):
    """Ubicaciones que la membresía puede ver o administrar (ORG-06): todas o su subárbol."""
    qs = Location.objects.all()
    location_ids = allowed_location_ids(membership)
    if location_ids is not None:
        qs = qs.filter(pk__in=location_ids)
    return qs


@dataclass
class TreeRow:
    location: Location
    depth: int
    path: str  # nombres de los ancestros, "Planta › Área"
    asset_count: int
    child_count: int


def location_tree(*, membership) -> list[TreeRow]:
    """Árbol de ubicaciones en orden de recorrido (padre antes que hijos), en una consulta.

    Con alcance por sitio, las raíces son las ubicaciones permitidas más altas.
    """
    locations = list(visible_locations(membership=membership).annotate(asset_count=Count("assets")))
    by_id = {location.pk: location for location in locations}
    children: dict = {}
    for location in locations:
        parent_id = location.parent_id if location.parent_id in by_id else None
        children.setdefault(parent_id, []).append(location)
    for siblings in children.values():
        siblings.sort(key=lambda loc: (loc.code.lower(), loc.name.lower()))

    rows: list[TreeRow] = []

    def walk(parent_id, depth, trail):
        for location in children.get(parent_id, []):
            rows.append(
                TreeRow(
                    location=location,
                    depth=depth,
                    path=" › ".join(trail),
                    asset_count=location.asset_count,
                    child_count=len(children.get(location.pk, [])),
                )
            )
            walk(location.pk, depth + 1, [*trail, location.name])

    walk(None, 0, [])
    return rows


def descendant_ids(location) -> set:
    """Ids de todas las ubicaciones bajo `location` (sin incluirla)."""
    children: dict = {}
    for location_id, parent_id in Location.objects.values_list("id", "parent_id"):
        children.setdefault(parent_id, []).append(location_id)
    found, pending = set(), [location.pk]
    while pending:
        for child in children.get(pending.pop(), []):
            if child not in found:
                found.add(child)
                pending.append(child)
    return found
