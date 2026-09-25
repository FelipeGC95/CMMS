from apps.accounts.scoping import allowed_location_ids

from .models import Location


def visible_locations(*, membership):
    """Ubicaciones visibles para la membresía (ORG-06): todas o su subárbol."""
    qs = Location.objects.all()
    location_ids = allowed_location_ids(membership)
    if location_ids is not None:
        qs = qs.filter(pk__in=location_ids)
    return qs


def descendant_ids(location) -> set:
    """Ids de todas las ubicaciones bajo `location` (sin incluirla), en una sola consulta."""
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
