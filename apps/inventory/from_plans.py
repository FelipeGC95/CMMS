"""«Crear catálogo desde los planes» (INV-11 CA3).

Agrupa las descripciones de los recursos tipo repuesto sin enlace de las
versiones vigentes y de los borradores (sin distinguir mayúsculas ni espacios
de más), propone un código `REP-0001`… y una unidad, y al confirmar crea los
repuestos y enlaza todos los recursos del grupo, incluidos los de versiones
publicadas (excepción acotada al ADR-016, `maintenance.services.link_resources`).

Si en el catálogo ya hay un repuesto activo con el mismo nombre normalizado,
el grupo se enlaza a él en vez de crear otro: repetir la operación no duplica.
"""

import re
from collections import Counter
from dataclasses import dataclass, field

from django.db import transaction
from django.db.models import F, Q

from apps.accounts.permissions import Perm, require_perm
from apps.maintenance.models import PlanResource, PlanVersion

from .models import Part
from .services import create_part

CODE_PREFIX = "REP-"
CODE_PATTERN = re.compile(r"^REP-(\d+)$", re.IGNORECASE)


def normalize(text: str) -> str:
    """Clave de agrupación: sin mayúsculas ni espacios de más."""
    return " ".join((text or "").split()).casefold()


@dataclass
class Proposal:
    key: str
    description: str
    unit: str
    code: str = ""
    existing: Part | None = None
    resources: list = field(default_factory=list)
    plans: list = field(default_factory=list)

    @property
    def resource_count(self) -> int:
        return len(self.resources)


def _unlinked_resources():
    """Recursos tipo repuesto sin enlace de versiones vigentes o borradores (una consulta)."""
    return (
        PlanResource.objects.filter(kind=PlanResource.Kind.PART, part__isnull=True)
        .filter(
            Q(version__status=PlanVersion.Status.DRAFT)
            | Q(version__status=PlanVersion.Status.PUBLISHED, version__plan__current_version=F("version_id"))
        )
        .select_related("version__plan")
        .order_by("version__plan__code", "created_at")
    )


def _next_codes(count: int) -> list[str]:
    """`count` códigos REP-NNNN libres, después del mayor REP- existente (una consulta)."""
    taken = {
        code.upper()
        for code in Part.objects.filter(code__istartswith=CODE_PREFIX).values_list("code", flat=True)
    }
    numbers = [int(m.group(1)) for code in taken if (m := CODE_PATTERN.match(code))]
    number, codes = max(numbers, default=0), []
    while len(codes) < count:
        number += 1
        code = f"{CODE_PREFIX}{number:04d}"
        if code not in taken:
            codes.append(code)
    return codes


def plan_part_proposals(*, membership) -> list[Proposal]:
    """Vista previa: un grupo por descripción normalizada, con código y unidad propuestos."""
    require_perm(membership, Perm.INVENTORY_MANAGE)
    groups: dict[str, Proposal] = {}
    units: dict[str, Counter] = {}
    for resource in _unlinked_resources():
        key = normalize(resource.description)
        if not key:
            continue
        proposal = groups.get(key)
        if proposal is None:
            proposal = groups[key] = Proposal(
                key=key, description=" ".join(resource.description.split())[:200], unit=""
            )
        proposal.resources.append(resource)
        plan_code = resource.version.plan.code
        if plan_code not in proposal.plans:
            proposal.plans.append(plan_code)
        if resource.unit.strip():
            units.setdefault(key, Counter())[resource.unit.strip()] += 1
    existing = {}
    for part in Part.objects.filter(is_active=True).order_by("code"):
        existing.setdefault(normalize(part.name), part)
    proposals = sorted(groups.values(), key=lambda p: p.key)
    new = [p for p in proposals if p.key not in existing]
    for proposal, code in zip(new, _next_codes(len(new)), strict=True):
        proposal.code = code
    for proposal in proposals:
        proposal.existing = existing.get(proposal.key)
        counter = units.get(proposal.key)
        proposal.unit = counter.most_common(1)[0][0] if counter else "un"
        if proposal.existing is not None:
            proposal.code, proposal.unit = proposal.existing.code, proposal.existing.unit
    return proposals


@transaction.atomic
def create_parts_from_plan_resources(*, membership, descriptions=None) -> tuple[int, int]:
    """Crea los repuestos propuestos y enlaza sus recursos. Devuelve (creados, recursos enlazados).

    `descriptions`: claves normalizadas (o descripciones) a incluir; `None` = todas.
    """
    from apps.maintenance.services import link_resources

    require_perm(membership, Perm.INVENTORY_MANAGE)
    wanted = None if descriptions is None else {normalize(d) for d in descriptions}
    created = linked = 0
    for proposal in plan_part_proposals(membership=membership):
        if wanted is not None and proposal.key not in wanted:
            continue
        part = proposal.existing
        if part is None:
            part = create_part(
                membership=membership, code=proposal.code, name=proposal.description, unit=proposal.unit
            )
            created += 1
        link_resources(proposal.resources, part=part, membership=membership)
        linked += len(proposal.resources)
    return created, linked
