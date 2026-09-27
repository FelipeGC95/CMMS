import re
from dataclasses import dataclass

from django.db import connection
from django.db.models import F, OuterRef, Q, Subquery, Sum
from django.db.models.functions import Coalesce

from apps.core.tenancy import platform_context
from apps.organizations.models import Organization

from .models import Attachment, DocumentPage, Purpose, TargetType

BYTES_PER_MB = 1024 * 1024
STORAGE_WARNING_RATIO = 0.8  # ORG-10 CA1: aviso al 80 % del cupo.


def attachments_for(*, target_type: str, target_id):
    """Documentos y fotos de un objeto; los manuales van en su propia sección (AST-06 CA3)."""
    return (
        Attachment.objects.filter(target_type=target_type, target_id=target_id)
        .exclude(purpose=Purpose.MANUAL)
        .select_related("uploaded_by")
    )


def attachments_by_target(*, target_type: str, target_ids) -> dict:
    """Adjuntos de varios objetos en una sola consulta: {target_id: [adjuntos]}."""
    grouped: dict = {}
    for attachment in Attachment.objects.filter(target_type=target_type, target_id__in=list(target_ids)):
        grouped.setdefault(attachment.target_id, []).append(attachment)
    return grouped


def storage_bytes_used(*, organization) -> int:
    """Bytes usados por la organización (AST-06 CA2): adjuntos ya comprimidos más el logo."""
    # all_tenants: se filtra explícitamente por la organización recibida.
    total = Attachment.all_tenants.filter(organization=organization).aggregate(total=Sum("size_bytes"))[
        "total"
    ]
    logo = Organization.objects.filter(pk=organization.pk).values_list("logo_size_bytes", flat=True).get()
    return (total or 0) + logo


@dataclass(frozen=True)
class StorageUsage:
    used_bytes: int
    quota_bytes: int | None  # None = plan sin cupo (ilimitado)

    @property
    def used_mb(self) -> float:
        return self.used_bytes / BYTES_PER_MB

    @property
    def quota_mb(self) -> int | None:
        return None if self.quota_bytes is None else self.quota_bytes // BYTES_PER_MB

    @property
    def ratio(self) -> float:
        if not self.quota_bytes:
            return 0.0 if self.quota_bytes is None else 1.0
        return self.used_bytes / self.quota_bytes

    @property
    def is_warning(self) -> bool:
        return self.quota_bytes is not None and self.ratio >= STORAGE_WARNING_RATIO

    @property
    def is_full(self) -> bool:
        return self.quota_bytes is not None and self.used_bytes >= self.quota_bytes

    def allows(self, extra_bytes: int) -> bool:
        return self.quota_bytes is None or self.used_bytes + extra_bytes <= self.quota_bytes


def storage_usage(*, organization) -> StorageUsage:
    """Uso (adjuntos + logo) y cupo del plan en una sola consulta (AST-06 CA2, ORG-10)."""
    # all_tenants: el filtro por organización es explícito (OuterRef) y no depende del contexto.
    used = (
        Attachment.all_tenants.filter(organization=OuterRef("pk"))
        .values("organization")
        .annotate(total=Sum("size_bytes"))
        .values("total")
    )
    # La subconsulta all_tenants va dentro de una consulta sobre Organization, que no
    # activa el bypass de RLS por sí sola (ADR-026): se hace explícito.
    with platform_context():
        quota_mb, used_bytes = (
            Organization.objects.filter(pk=organization.pk)
            .annotate(used=Coalesce(Subquery(used), 0) + F("logo_size_bytes"))
            .values_list("plan__max_storage_mb", "used")
            .get()
        )
    return StorageUsage(
        used_bytes=used_bytes,
        quota_bytes=None if quota_mb is None else quota_mb * BYTES_PER_MB,
    )


# --- Manuales (AST-06 CA3) ---------------------------------------------------------------

SEARCH_TERM_LIMIT = 8
SNIPPET_CHARS = 220


def manuals_for_asset(*, asset):
    """Manuales propios del activo más los de su clase (los de la clase aplican a todos sus activos).

    El activo ya viene de `visible_assets`: quien lo ve, lee sus manuales.
    """
    return (
        Attachment.objects.filter(purpose=Purpose.MANUAL)
        .filter(
            Q(target_type=TargetType.ASSET, target_id=asset.pk)
            | Q(target_type=TargetType.ASSET_CLASS, target_id=asset.asset_class_id)
        )
        .select_related("uploaded_by")
        .order_by("target_type", "filename")
    )


def manuals_for_class(*, asset_class):
    return (
        Attachment.objects.filter(
            purpose=Purpose.MANUAL, target_type=TargetType.ASSET_CLASS, target_id=asset_class.pk
        )
        .select_related("uploaded_by")
        .order_by("filename")
    )


def visible_manuals(*, membership):
    """Manuales que la membresía puede leer, con el alcance por sitio (ORG-06).

    De un activo: si el activo es visible. De una clase: sin restricción de sitio
    (o con permiso para gestionar activos) se ven todos; con restricción, solo los
    de clases con algún activo visible.
    """
    from apps.accounts.permissions import Perm, has_perm
    from apps.accounts.scoping import allowed_location_ids
    from apps.assets.models import AssetClass
    from apps.assets.selectors import visible_assets

    assets = visible_assets(membership=membership)
    if allowed_location_ids(membership) is None or has_perm(membership, Perm.ASSET_MANAGE):
        classes = AssetClass.objects.values("pk")
    else:
        classes = assets.values("asset_class_id")
    return Attachment.objects.filter(purpose=Purpose.MANUAL).filter(
        Q(target_type=TargetType.ASSET, target_id__in=assets.values("pk"))
        | Q(target_type=TargetType.ASSET_CLASS, target_id__in=classes)
    )


def search_terms(query: str) -> list[str]:
    return [term for term in re.findall(r"\w+", query.lower()) if len(term) > 1][:SEARCH_TERM_LIMIT]


def search_manual_pages(*, membership, query: str):
    """Páginas de manuales visibles que contienen la consulta (AST-06 CA3).

    PostgreSQL: texto completo en español, ordenado por relevancia. Otros motores
    (SQLite local): todas las palabras con `icontains`.
    """
    terms = search_terms(query)
    pages = DocumentPage.objects.filter(attachment__in=visible_manuals(membership=membership)).select_related(
        "attachment"
    )
    if not terms:
        return pages.none()
    if connection.vendor == "postgresql":
        from django.contrib.postgres.search import SearchQuery, SearchRank, SearchVector

        vector = SearchVector("text", config="spanish")
        search = SearchQuery(" ".join(terms), config="spanish")
        return (
            pages.annotate(search=vector, rank=SearchRank(vector, search))
            .filter(search=search)
            .order_by("-rank", "attachment__filename", "page_number")
        )
    for term in terms:
        pages = pages.filter(text__icontains=term)
    return pages.order_by("attachment__filename", "page_number")


def snippet(text: str, terms: list[str], *, length: int = SNIPPET_CHARS) -> str:
    """Fragmento alrededor de la primera coincidencia (texto plano: la plantilla lo escapa)."""
    lowered = text.lower()
    positions = [lowered.find(term) for term in terms if term in lowered]
    start = max(0, min(positions) - length // 3) if positions else 0
    fragment = " ".join(text[start : start + length].split())
    return ("…" if start else "") + fragment + ("…" if start + length < len(text) else "")


def manual_targets(attachments) -> dict:
    """{target_id: activo o clase} de una página de resultados, en dos consultas como máximo."""
    from apps.assets.models import Asset, AssetClass

    ids = {TargetType.ASSET: set(), TargetType.ASSET_CLASS: set()}
    for attachment in attachments:
        ids.setdefault(attachment.target_type, set()).add(attachment.target_id)
    targets = {}
    if ids[TargetType.ASSET]:
        targets.update(Asset.objects.in_bulk(ids[TargetType.ASSET]))
    if ids[TargetType.ASSET_CLASS]:
        targets.update(AssetClass.objects.in_bulk(ids[TargetType.ASSET_CLASS]))
    return targets
