"""Alcance por sitio de una membresía (ORG-06).

`allowed_location_ids` es la API estable que usan los selectores (y la
importación Excel) para limitar lo que ve o toca una membresía.
"""

from collections import defaultdict

from apps.organizations.models import Location

from .models import Membership, MembershipLocation


def is_scoped(membership: Membership) -> bool:
    """El rol externo siempre está limitado; los demás, solo si se marcó en la membresía."""
    return membership.role == Membership.Role.EXTERNAL or membership.restricted_to_locations


def allowed_location_ids(membership: Membership | None) -> frozenset | None:
    """Ubicaciones visibles para la membresía, incluidas todas sus descendientes.

    `None` significa sin restricción (toda la organización); un conjunto vacío,
    que no ve nada. Requiere el contexto de tenant de la organización de la
    membresía. El resultado se guarda en la instancia (una vez por petición).
    """
    if membership is None:
        return frozenset()
    if not is_scoped(membership):
        return None
    cached = getattr(membership, "_allowed_location_ids", None)
    if cached is not None:
        return cached

    roots = set(
        MembershipLocation.objects.filter(membership=membership).values_list("location_id", flat=True)
    )
    allowed = set(roots)
    if roots:
        children = defaultdict(list)
        for location_id, parent_id in Location.objects.values_list("id", "parent_id"):
            children[parent_id].append(location_id)
        pending = list(roots)
        while pending:
            for child in children[pending.pop()]:
                if child not in allowed:
                    allowed.add(child)
                    pending.append(child)
    membership._allowed_location_ids = frozenset(allowed)
    return membership._allowed_location_ids
