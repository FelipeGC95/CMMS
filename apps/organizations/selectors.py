import math
from dataclasses import dataclass

from django.db.models import Count, Sum
from django.utils.translation import gettext as _

from apps.accounts.models import Membership
from apps.accounts.permissions import Perm, has_perm
from apps.accounts.scoping import allowed_location_ids
from apps.accounts.selectors import pending_invitations_qs
from apps.assets.models import Asset
from apps.attachments.models import Attachment

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


@dataclass
class QuotaRow:
    label: str
    used: int
    limit: int | None  # None = ilimitado

    @property
    def is_over(self) -> bool:
        return self.limit is not None and self.used > self.limit

    @property
    def is_near(self) -> bool:
        """Al 80 % o más del cupo (ORG-10 CA1), sin pasarse."""
        return self.limit is not None and not self.is_over and self.used >= self.limit * 0.8


def organization_quotas(*, membership) -> list[QuotaRow]:
    """Uso de los cupos del plan de la organización activa (solo lectura, ORG-01 CA1).

    Usuarios cuenta miembros activos + invitaciones pendientes, igual que al invitar (ORG-05 CA3).
    """
    organization = membership.organization
    plan = organization.plan
    users = Membership.objects.filter(organization=organization, is_active=True).count()
    users += pending_invitations_qs(organization).count()
    storage_bytes = Attachment.objects.aggregate(total=Sum("size_bytes"))["total"] or 0
    return [
        QuotaRow(_("Activos"), Asset.objects.count(), plan.max_assets if plan else None),
        QuotaRow(_("Usuarios"), users, plan.max_users if plan else None),
        QuotaRow(
            _("Almacenamiento (MB)"),
            math.ceil(storage_bytes / (1024 * 1024)),
            plan.max_storage_mb if plan else None,
        ),
    ]


def recent_data_exports(*, membership, limit=5) -> list:
    """Últimas exportaciones completas (X-03) para la tarjeta de Configuración."""
    from .models import DataExport

    if not has_perm(membership, Perm.ORGANIZATION_MANAGE):
        return []
    return list(DataExport.objects.select_related("requested_by").order_by("-created_at")[:limit])
